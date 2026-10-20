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
# 컬렉션마다 찾는 사실의 종류가 달라 지시문도 컬렉션별로 둔다.
QUERY_INSTRUCT = {
    R1: "회의·면접 발언 중 질문과 관련된 발언을 찾는다",
    R2: "프로젝트 기준 문서에서 질문과 관련된 조항을 찾는다",
    R3: "결정 원장에서 같은 주제의 결정을 찾는다",
    R4: "사내 정책에서 해당 행위에 적용되는 규칙을 찾는다",
}


def embed(texts: list[str]) -> list[list[float]]:
    """
    Ollama 임베딩 API로 문장 목록을 벡터로 변환
    Args:
        texts: 임베딩할 문장 목록
    Returns:
        입력 순서와 같은 순서의 임베딩 벡터 목록
    """
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    # 로컬 임베딩은 모델 적재·배치 처리에 시간이 걸릴 수 있어 제한 시간을 넉넉히 둔다
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def _q(collection: str, query: str) -> str:
    """
    질의에 컬렉션별 지시문을 붙인 qwen3-embedding 질의 형식 생성
    Args:
        collection: 컬렉션 이름 (R1~R4)
        query:      사용자 질의
    Returns:
        "Instruct: …\\nQuery: …" 형식의 질의 문자열
    """
    return f"Instruct: {QUERY_INSTRUCT[collection]}\nQuery: {query}"


class LedgerRAG:
    """
    R1~R4 Chroma 컬렉션의 적재·검색 창구

    벡터 DB는 후보 찾기용. "지금 유효한가"의 최종 판단은 SQLite(LedgerStore) 담당.
    """

    def __init__(self, path: Path | str = CHROMA_DIR):
        """
        Chroma 영속 클라이언트 생성
        Args:
            path: Chroma 저장 디렉터리 (없으면 생성)
        """
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path))

    def col(self, name: str):
        """
        이름으로 컬렉션을 가져오거나 새로 생성
        Args:
            name: 컬렉션 이름
        Returns:
            Chroma 컬렉션 객체
        """
        # 거리 계산을 cosine으로 고정해 _search에서 1 - distance를 유사도로 쓸 수 있게 한다
        return self.client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})

    def reset(self, names: list[str] | None = None) -> None:
        """
        컬렉션 삭제 (재적재 전 초기화)
        Args:
            names: 지울 컬렉션 이름 목록. None이면 R1~R4 전부
        """
        for n in names or [R1, R2, R3, R4]:
            try:
                self.client.delete_collection(n)
            except Exception:  # pylint: disable=broad-exception-caught  # chromadb 버전마다 없는 컬렉션 예외 종류가 다름
                # 아직 만들어지지 않은 컬렉션은 지울 것이 없으므로 무시
                pass

    def _upsert(self, name: str, ids: list[str], docs: list[str], metas: list[dict], batch: int = 32) -> int:
        """
        문서를 임베딩해 컬렉션에 배치 단위로 upsert
        Args:
            name:  컬렉션 이름
            ids:   레코드 ID 목록
            docs:  임베딩할 본문 목록
            metas: 메타데이터 목록
            batch: 한 번에 임베딩·적재할 건수
        Returns:
            적재한 레코드 수
        """
        c = self.col(name)
        # 임베딩 요청 1회당 입력 크기를 제한하려고 batch 단위로 나눈다.
        # upsert라 같은 ID를 다시 넣으면 덮어써서 재적재해도 중복이 생기지 않는다.
        for i in range(0, len(ids), batch):
            c.upsert(ids=ids[i:i + batch], documents=docs[i:i + batch],
                     metadatas=metas[i:i + batch], embeddings=embed(docs[i:i + batch]))
        return len(ids)

    # ---------------- 적재 ----------------
    def ingest_transcript(self, t: dict, domain: str = "meeting") -> int:
        """
        원문 1건(회의·면접)을 발언 단위로 R1에 적재

        앞뒤 발언을 함께 넣어 문맥을 살린다.
        Args:
            t:      원문 레코드 (meeting_id, project_id, date, segments 포함)
            domain: meeting 또는 interview
        Returns:
            적재한 발언 수
        """
        segs = t["segments"]
        ids, docs, metas = [], [], []
        for i, s in enumerate(segs):
            prev_ = segs[i - 1]["text"] if i > 0 else ""
            next_ = segs[i + 1]["text"] if i + 1 < len(segs) else ""
            # ID = 회의ID#발언번호 — 원문 인용(evidence)의 {meeting_id, seq}와 1:1 대응
            ids.append(f"{t['meeting_id']}#{s['seq']}")
            # 앞뒤 문맥은 80자로 잘라 본 발언보다 비중이 커지지 않게 한다
            docs.append(f"[{s['speaker']}/{s['role']}] {s['text']}\n(앞: {prev_[:80]})\n(뒤: {next_[:80]})")
            # 메타의 text에는 문맥 없는 원문만 둔다 (인용은 원문 그대로여야 하므로)
            metas.append({"domain": domain, "project_id": t["project_id"], "meeting_id": t["meeting_id"],
                          "date": t["date"], "seq": s["seq"], "ts": s["ts"], "speaker": s["speaker"],
                          "role": s["role"], "text": s["text"]})
        return self._upsert(R1, ids, docs, metas)

    def ingest_baseline_doc(self, md_text: str, doc: dict, project_id: str, domain: str = "meeting") -> int:
        """
        기준 문서를 '## ' 섹션 단위로 R2에 적재

        섹션 안 'topic:' 줄을 주제 키로 쓴다. 섹션이 없는 문서(용어집)는 표 한 줄을 한 청크로.
        Args:
            md_text:    문서 마크다운 본문
            doc:        문서 목록 레코드 (doc_id, doc_version, title, is_current)
            project_id: 문서가 속한 프로젝트 ID
            domain:     meeting 또는 interview
        Returns:
            적재한 청크 수
        """
        # '## ' 앞에서만 자르는 lookahead — 제목 줄을 각 청크 첫 줄에 남기기 위함
        parts = re.split(r"\n(?=## )", md_text)
        ids, docs, metas = [], [], []
        for p in parts:
            # 첫 '## ' 이전의 머리말(문서 제목·서문)은 조항이 아니므로 제외
            if not p.startswith("## "):
                continue
            title = p.splitlines()[0][3:].strip()
            # topic: 줄은 결정·정책과 같은 주제 키로 쓰는 메타데이터라 본문에서는 뺀다
            m = re.search(r"^topic:\s*(\S+)", p, re.M)
            body = re.sub(r"^topic:.*$", "", p, flags=re.M).strip()
            # 버전을 ID에 넣어 구버전·현행 조항이 서로 덮어쓰지 않게 한다 (제목은 30자로 잘라 키 길이 제한)
            sid = f"{doc['doc_id']}@{doc['doc_version']}#{title[:30]}"
            ids.append(sid)
            docs.append(f"{doc['title']} {doc['doc_version']} — {body}")
            metas.append({"domain": domain, "project_id": project_id, "doc_id": doc["doc_id"],
                          "doc_version": doc["doc_version"], "is_current": bool(doc["is_current"]),
                          "section": title, "topic": m.group(1) if m else "", "text": body})
        # 용어집처럼 섹션이 없는 문서는 표 한 줄을 한 청크로
        if not ids:
            for line in md_text.splitlines():
                # 표 구분선(|---|)과 머리글 행("표준 용어")은 내용이 없어 건너뛴다
                if line.startswith("|") and not set(line) <= set("|- ") and "표준 용어" not in line:
                    term = line.strip("|").split("|")[0].strip()
                    ids.append(f"{doc['doc_id']}@{doc['doc_version']}#{term}")
                    docs.append(line.strip())
                    # topic=glossary는 주제 후보 계산(Resolver.topic_candidates)에서 제외하는 표식
                    metas.append({"domain": domain, "project_id": project_id, "doc_id": doc["doc_id"],
                                  "doc_version": doc["doc_version"], "is_current": bool(doc["is_current"]),
                                  "section": term, "topic": "glossary", "text": line.strip()})
        return self._upsert(R2, ids, docs, metas)

    def ingest_decision(self, d: dict, domain: str = "meeting") -> int:
        """
        결정 1건을 R3에 적재 (승인·반려·대체 뒤 재적재에도 사용)
        Args:
            d:      결정 레코드 (LedgerStore.get_decision 형식)
            domain: meeting 또는 interview
        Returns:
            적재한 레코드 수 (1)
        """
        alts = ", ".join(d.get("alternatives_rejected") or [])
        # 이유·기각한 대안까지 본문에 넣어 "왜 그렇게 정했나" 질문에도 걸리게 한다
        doc = (f"[{d['topic']}] {d['statement']} / 이유: {d.get('rationale') or ''}"
               f" / 기각한 대안: {alts}")
        # active = 승인 + 대체 안 됨. 검색 기본 필터가 이 값을 쓴다 (store._row_to_decision의 is_active와 같은 규칙)
        active = d["status"] == "approved" and not d.get("superseded_by")
        # Chroma 메타데이터는 None을 받지 않아 빈 문자열로 바꾼다
        meta = {"domain": domain, "project_id": d["project_id"], "decision_id": d["decision_id"],
                "topic": d["topic"], "status": d["status"], "superseded_by": d.get("superseded_by") or "",
                "active": active, "effective_from": d.get("effective_from") or "",
                "statement": d["statement"]}
        return self._upsert(R3, [d["decision_id"]], [doc], [meta])

    def ingest_rule(self, r: dict, domain: str = "meeting") -> int:
        """
        정책 규칙 1건을 R4에 적재
        Args:
            r:      규칙 레코드 (policy_version, rule_id, is_current, domain, topic, text, approver_role)
            domain: meeting 또는 interview
        Returns:
            적재한 레코드 수 (1)
        """
        # 같은 rule_id라도 버전별로 따로 보관해야 구 정책 간섭을 재현할 수 있어 버전을 ID에 넣는다
        rid = f"{r['policy_version']}:{r['rule_id']}"
        doc = f"[{r['rule_id']} · {r['domain']}] {r['text']} (승인자: {r['approver_role']})"
        # 규칙의 domain(authority 등)은 레코드 domain(meeting|interview)과 겹치지 않게 rule_domain으로 저장
        meta = {"domain": domain, "rule_id": r["rule_id"], "policy_version": r["policy_version"],
                "is_current": bool(r["is_current"]), "rule_domain": r["domain"], "topic": r["topic"],
                "approver_role": r["approver_role"] or "", "text": r["text"]}
        return self._upsert(R4, [rid], [doc], [meta])

    # ---------------- 검색 ----------------
    def _search(self, name: str, query: str, where: dict | None, k: int) -> list[dict]:
        """
        컬렉션 공통 벡터 검색
        Args:
            name:  컬렉션 이름
            query: 사용자 질의 (지시문은 여기서 붙인다)
            where: Chroma 메타데이터 필터. None이면 필터 없음
            k:     최대 결과 수
        Returns:
            메타데이터에 _id와 _score(cosine 유사도)를 더한 결과 목록, 유사도 순
        """
        res = self.col(name).query(query_embeddings=embed([_q(name, query)]), n_results=k, where=where)
        out = []
        for i in range(len(res["ids"][0])):
            m = dict(res["metadatas"][0][i])
            m["_id"] = res["ids"][0][i]
            m["_score"] = round(1 - res["distances"][0][i], 4)   # cosine 거리 → 유사도 (높을수록 가까움)
            out.append(m)
        return out

    @staticmethod
    def _and(*conds: dict | None) -> dict | None:
        """
        None이 아닌 조건만 모아 Chroma where 필터로 결합
        Args:
            conds: 개별 조건 (None은 무시)
        Returns:
            조건이 없으면 None, 하나면 그 조건, 둘 이상이면 {"$and": [...]}
        """
        cs = [c for c in conds if c]
        if not cs:
            return None
        # Chroma는 조건이 하나뿐인 $and를 거부해서 단일 조건은 그대로 돌려준다
        return cs[0] if len(cs) == 1 else {"$and": cs}

    def search_transcript(self, query: str, meeting_id: str | None = None, project_id: str | None = None,
                          k: int = 5, min_score: float | None = None) -> list[dict]:
        """
        R1 원문 발언 검색
        Args:
            query:      찾을 내용
            meeting_id: 특정 회의로 한정. None이면 전체
            project_id: 특정 프로젝트로 한정. None이면 전체
            k:          최대 결과 수
            min_score:  이 유사도 미만 발언은 버린다. None이면 거르지 않음
        Returns:
            발언 메타데이터 목록 (_id, _score 포함)
        """
        rows = self._search(R1, query, self._and(
            {"meeting_id": meeting_id} if meeting_id else None,
            {"project_id": project_id} if project_id else None), k)
        # 하한이 없으면 논의한 적 없는 주제에도 가장 가까운 발언이 '관련 논의'로 붙는다
        return [r for r in rows if min_score is None or r["_score"] >= min_score]

    def delete_transcript(self, meeting_id: str) -> int:
        """
        회의 하나의 발언 색인 삭제 (보존 기한이 지난 원문 파기용, 정책 SEC-02)

        원장의 인용은 남는다.
        Args:
            meeting_id: 회의 ID
        Returns:
            지운 발언 수
        """
        col = self.col(R1)
        ids = col.get(where={"meeting_id": meeting_id})["ids"]
        if ids:
            col.delete(ids=ids)
        return len(ids)

    def topic_support(self, query: str, topic: str) -> float | None:
        """
        이 주제로 저장된 결정·기준 문서 중 질의와 가장 가까운 것의 유사도

        LLM이 고른 주제가 문장 내용과 맞는지 독립적으로 확인하는 데 쓴다.
        Args:
            query: 결정·의견 문장
            topic: 확인할 주제 키
        Returns:
            최고 유사도. 그 주제로 저장된 결정·기준 문서가 하나도 없으면 None
        """
        scores = []
        for name in (R3, R2):
            scores += [r["_score"] for r in self._search(name, query, {"topic": topic}, 1)]
        return max(scores) if scores else None

    def search_baseline(self, query: str, project_id: str | None = None, k: int = 4,
                        filtered: bool = True) -> list[dict]:
        """
        R2 기준 문서 조항 검색
        Args:
            query:      찾을 내용
            project_id: 특정 프로젝트로 한정. None이면 전체
            k:          최대 결과 수
            filtered:   True면 현행 버전(is_current)만. False는 구버전 간섭 재현용
        Returns:
            조항 메타데이터 목록 (_id, _score 포함)
        """
        return self._search(R2, query, self._and(
            {"is_current": True} if filtered else None,
            {"project_id": project_id} if project_id else None), k)

    def search_decisions(self, query: str, project_id: str | None = None, k: int = 5,
                         filtered: bool = True, include_proposed: bool = False) -> list[dict]:
        """
        R3 결정 원장 검색

        기본은 유효 결정(active)만. include_proposed면 승인 대기도 함께(권위는 없음).
        Args:
            query:            찾을 내용
            project_id:       특정 프로젝트로 한정. None이면 전체
            k:                최대 결과 수
            filtered:         False면 대체·반려 결정까지 전부 (간섭 재현용)
            include_proposed: True면 승인 대기(proposed)도 포함
        Returns:
            결정 메타데이터 목록 (_id, _score 포함)
        """
        status_cond = None
        if filtered:
            # 반려(rejected)·대체된 결정은 어느 경우에도 빠진다
            status_cond = ({"$or": [{"active": True}, {"status": "proposed"}]}
                           if include_proposed else {"active": True})
        return self._search(R3, query, self._and(
            status_cond, {"project_id": project_id} if project_id else None), k)

    def search_rules(self, query: str, k: int = 4, filtered: bool = True, domain: str | None = None) -> list[dict]:
        """
        R4 정책 규칙 검색
        Args:
            query:    찾을 내용
            k:        최대 결과 수
            filtered: True면 현행 정책(is_current)만. False는 구 정책 간섭 재현용
            domain:   레코드 domain(meeting|interview)으로 한정. None이면 전체
        Returns:
            규칙 메타데이터 목록 (_id, _score 포함)
        """
        return self._search(R4, query, self._and(
            {"is_current": True} if filtered else None,
            {"domain": domain} if domain else None), k)

    def counts(self) -> dict:
        """
        컬렉션별 적재 건수
        Returns:
            {컬렉션 이름: 건수}
        """
        return {n: self.col(n).count() for n in (R1, R2, R3, R4)}
