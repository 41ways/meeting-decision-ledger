"""RAG 4종 — Chroma 컬렉션 적재와 검색.

컬렉션은 담는 사실의 종류로 나눈다 (docs/design.md §2).
  r1_transcripts : 원문 발언 (증거). 권위 없음, 인용용.
  r2_baseline    : 기준 문서 섹션. is_current=True만 검색.
  r3_ledger      : 결정·평가 원장 레코드. 기본은 active(승인 + 대체 안 됨)만 검색.
  r4_governance  : 정책 규칙. 현행 버전만 검색.

모든 레코드에 domain(meeting|interview)과 project_id를 붙여 같은 구조로 두 도메인을 받는다.
필터를 끄는 옵션(filtered=False)은 평가에서 "옛 결과 간섭"을 재현하기 위한 것이다.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import chromadb

ROOT = Path(__file__).resolve().parent.parent
CHROMA_DIR = Path(os.environ.get("LEDGER_STORE", ROOT / "store")) / "chroma"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding:4b")

R1, R2, R3, R4 = "r1_transcripts", "r2_baseline", "r3_ledger", "r4_governance"

# qwen3-embedding은 질의 쪽에 지시문을 붙이면 검색 품질이 좋아진다 (문서 쪽은 그대로).
QUERY_INSTRUCT = {
    R1: "회의·면접 발언 중 질문과 관련된 발언을 찾는다",
    R2: "프로젝트 기준 문서에서 질문과 관련된 조항을 찾는다",
    R3: "결정 원장에서 같은 주제의 결정을 찾는다",
    R4: "사내 정책에서 해당 행위에 적용되는 규칙을 찾는다",
}


def embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def _q(collection: str, query: str) -> str:
    return f"Instruct: {QUERY_INSTRUCT[collection]}\nQuery: {query}"


class LedgerRAG:
    def __init__(self, path: Path | str = CHROMA_DIR):
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path))

    def col(self, name: str):
        return self.client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})

    def reset(self, names: list[str] | None = None) -> None:
        for n in names or [R1, R2, R3, R4]:
            try:
                self.client.delete_collection(n)
            except Exception:
                pass

    def _upsert(self, name: str, ids: list[str], docs: list[str], metas: list[dict], batch: int = 32) -> int:
        c = self.col(name)
        for i in range(0, len(ids), batch):
            c.upsert(ids=ids[i:i + batch], documents=docs[i:i + batch],
                     metadatas=metas[i:i + batch], embeddings=embed(docs[i:i + batch]))
        return len(ids)

    # ---------------- 적재 ----------------
    def ingest_transcript(self, t: dict, domain: str = "meeting") -> int:
        """원문 1건(회의·면접)을 발언 단위로 넣는다. 앞뒤 발언을 함께 넣어 문맥을 살린다."""
        segs = t["segments"]
        ids, docs, metas = [], [], []
        for i, s in enumerate(segs):
            prev_ = segs[i - 1]["text"] if i > 0 else ""
            next_ = segs[i + 1]["text"] if i + 1 < len(segs) else ""
            ids.append(f"{t['meeting_id']}#{s['seq']}")
            docs.append(f"[{s['speaker']}/{s['role']}] {s['text']}\n(앞: {prev_[:80]})\n(뒤: {next_[:80]})")
            metas.append({"domain": domain, "project_id": t["project_id"], "meeting_id": t["meeting_id"],
                          "date": t["date"], "seq": s["seq"], "ts": s["ts"], "speaker": s["speaker"],
                          "role": s["role"], "text": s["text"]})
        return self._upsert(R1, ids, docs, metas)

    def delete_transcript(self, meeting_id: str) -> None:
        """보존 기한이 지난 원문 파기 (정책 SEC-02). 원장의 인용은 남는다."""
        self.col(R1).delete(where={"meeting_id": meeting_id})

    def ingest_baseline_doc(self, md_text: str, doc: dict, project_id: str, domain: str = "meeting") -> int:
        """기준 문서를 '## ' 섹션 단위로 넣는다. 섹션 안 'topic:' 줄을 주제 키로 쓴다."""
        parts = re.split(r"\n(?=## )", md_text)
        ids, docs, metas = [], [], []
        for p in parts:
            if not p.startswith("## "):
                continue
            title = p.splitlines()[0][3:].strip()
            m = re.search(r"^topic:\s*(\S+)", p, re.M)
            body = re.sub(r"^topic:.*$", "", p, flags=re.M).strip()
            sid = f"{doc['doc_id']}@{doc['doc_version']}#{title[:30]}"
            ids.append(sid)
            docs.append(f"{doc['title']} {doc['doc_version']} — {body}")
            metas.append({"domain": domain, "project_id": project_id, "doc_id": doc["doc_id"],
                          "doc_version": doc["doc_version"], "is_current": bool(doc["is_current"]),
                          "section": title, "topic": m.group(1) if m else "", "text": body})
        # 용어집처럼 섹션이 없는 문서는 표 한 줄을 한 청크로
        if not ids:
            for line in md_text.splitlines():
                if line.startswith("|") and not set(line) <= set("|- ") and "표준 용어" not in line:
                    term = line.strip("|").split("|")[0].strip()
                    ids.append(f"{doc['doc_id']}@{doc['doc_version']}#{term}")
                    docs.append(line.strip())
                    metas.append({"domain": domain, "project_id": project_id, "doc_id": doc["doc_id"],
                                  "doc_version": doc["doc_version"], "is_current": bool(doc["is_current"]),
                                  "section": term, "topic": "glossary", "text": line.strip()})
        return self._upsert(R2, ids, docs, metas)

    def ingest_decision(self, d: dict, domain: str = "meeting") -> int:
        alts = ", ".join(d.get("alternatives_rejected") or [])
        doc = (f"[{d['topic']}] {d['statement']} / 이유: {d.get('rationale') or ''}"
               f" / 기각한 대안: {alts}")
        active = d["status"] == "approved" and not d.get("superseded_by")
        meta = {"domain": domain, "project_id": d["project_id"], "decision_id": d["decision_id"],
                "topic": d["topic"], "status": d["status"], "superseded_by": d.get("superseded_by") or "",
                "active": active, "effective_from": d.get("effective_from") or "",
                "statement": d["statement"]}
        return self._upsert(R3, [d["decision_id"]], [doc], [meta])

    def ingest_rule(self, r: dict, domain: str = "meeting") -> int:
        rid = f"{r['policy_version']}:{r['rule_id']}"
        doc = f"[{r['rule_id']} · {r['domain']}] {r['text']} (승인자: {r['approver_role']})"
        meta = {"domain": domain, "rule_id": r["rule_id"], "policy_version": r["policy_version"],
                "is_current": bool(r["is_current"]), "rule_domain": r["domain"], "topic": r["topic"],
                "approver_role": r["approver_role"] or "", "text": r["text"]}
        return self._upsert(R4, [rid], [doc], [meta])

    # ---------------- 검색 ----------------
    def _search(self, name: str, query: str, where: dict | None, k: int) -> list[dict]:
        res = self.col(name).query(query_embeddings=embed([_q(name, query)]), n_results=k, where=where)
        out = []
        for i in range(len(res["ids"][0])):
            m = dict(res["metadatas"][0][i])
            m["_id"] = res["ids"][0][i]
            m["_score"] = round(1 - res["distances"][0][i], 4)   # cosine 유사도
            out.append(m)
        return out

    @staticmethod
    def _and(*conds: dict | None) -> dict | None:
        cs = [c for c in conds if c]
        if not cs:
            return None
        return cs[0] if len(cs) == 1 else {"$and": cs}

    def search_transcript(self, query: str, meeting_id: str | None = None, project_id: str | None = None,
                          k: int = 5) -> list[dict]:
        return self._search(R1, query, self._and(
            {"meeting_id": meeting_id} if meeting_id else None,
            {"project_id": project_id} if project_id else None), k)

    def search_baseline(self, query: str, project_id: str | None = None, k: int = 4,
                        filtered: bool = True) -> list[dict]:
        return self._search(R2, query, self._and(
            {"is_current": True} if filtered else None,
            {"project_id": project_id} if project_id else None), k)

    def search_decisions(self, query: str, project_id: str | None = None, k: int = 5,
                         filtered: bool = True, include_proposed: bool = False) -> list[dict]:
        """기본: 유효 결정(active)만. include_proposed면 승인 대기도 함께(권위는 없음)."""
        status_cond = None
        if filtered:
            status_cond = ({"$or": [{"active": True}, {"status": "proposed"}]}
                           if include_proposed else {"active": True})
        return self._search(R3, query, self._and(
            status_cond, {"project_id": project_id} if project_id else None), k)

    def search_rules(self, query: str, k: int = 4, filtered: bool = True, domain: str | None = None) -> list[dict]:
        return self._search(R4, query, self._and(
            {"is_current": True} if filtered else None,
            {"domain": domain} if domain else None), k)

    def counts(self) -> dict:
        return {n: self.col(n).count() for n in (R1, R2, R3, R4)}
