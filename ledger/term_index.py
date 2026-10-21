"""낱말 색인 — 결정·미결을 주제·별칭·ID로, 회의 원문을 단어로 찾는다 (벡터 DB를 쓰지 않는다).

결정·미결 찾기 (term_index 테이블)
  항목마다 '찾는 말(term)'을 미리 적어 둔다. 질문에서 나온 말과 같은 줄만 꺼낸다.
    t:<주제 키>  항목의 주제, 또는 문장에 나온 별칭이 가리키는 주제
    d:<결정 ID>  결정 자신, 미결이 이어진 결정
    m:<회의 ID>  결정의 근거 회의, 미결이 나온 회의
    w:<낱말>     문장의 내용어 (주제·ID로 못 찾을 때만 쓰는 둘째 단계)
  줄마다 via(왜 이 말이 붙었나)를 남겨 "OPS-0008은 별칭 '대화 저장' 때문에 후보" 처럼 설명할 수 있다.
  원장·회의 기록이 바뀌면 그 항목 줄만 다시 쓴다 (store·records가 같은 연결 안에서 부른다).

회의 원문 찾기 (r1_seg·r1_term 테이블)
  발언마다 내용어와 별칭 주제 표식(#주제)을 적고 BM25로 순위를 매긴다.
  실제 회의엔 잡담·주제 이탈이 많아 의미가 비슷한 엉뚱한 발언이 붙기 쉬워서, 낱말이 실제로 겹치는 발언만 돌려준다.

같은 ledger.db 파일을 쓴다. 별칭 파일·용어집이 바뀌면(aliases.version) 색인을 처음부터 다시 만든다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import aliases as A

SCHEMA = """
CREATE TABLE IF NOT EXISTS term_index (
    term TEXT NOT NULL,              -- t:주제 · d:결정 ID · m:회의 ID · w:낱말
    kind TEXT NOT NULL,              -- D(원장 결정) | O(미결 항목)
    ref TEXT NOT NULL,               -- 결정 ID 또는 항목 ID
    project_id TEXT,
    via TEXT,                        -- 이 말이 붙은 까닭 (주제 · 별칭 '…' · 결정 ID · 근거 회의 · 단어)
    PRIMARY KEY (term, kind, ref, via)
);
CREATE INDEX IF NOT EXISTS term_index_ref ON term_index(kind, ref);
CREATE TABLE IF NOT EXISTS index_meta (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS r1_seg (
    meeting_id TEXT NOT NULL, seq INTEGER NOT NULL,
    project_id TEXT, date TEXT, ts TEXT, speaker TEXT, role TEXT, text TEXT,
    n_terms INTEGER NOT NULL,
    PRIMARY KEY (meeting_id, seq)
);
CREATE TABLE IF NOT EXISTS r1_term (
    term TEXT NOT NULL, meeting_id TEXT NOT NULL, seq INTEGER NOT NULL, tf INTEGER NOT NULL,
    PRIMARY KEY (term, meeting_id, seq)
);
CREATE TABLE IF NOT EXISTS r1_file (meeting_id TEXT PRIMARY KEY, sig TEXT);
"""

DECISION_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}-\d{4}(?!\d)")


def _json(v, default):
    """
    JSON 문자열이면 풀고, 이미 풀린 값이면 그대로
    Args:
        v:       DB 컬럼 값 또는 dict 값
        default: 비었을 때 값
    Returns:
        풀린 값
    """
    if isinstance(v, str):
        return json.loads(v) if v else default
    return v if v is not None else default


def _alias_terms(text: str) -> list[tuple[str, str]]:
    """
    문장 속 별칭이 가리키는 주제 줄
    Args:
        text: 문장
    Returns:
        [(t:주제, via)]
    """
    return [(f"t:{h['topic']}", f"별칭 '{h['aliases'][0]}'") for h in A.topics_of(text)]


def ensure(c: sqlite3.Connection, kinds: tuple[str, ...]) -> None:
    """
    색인 테이블을 만들고, 별칭 판이 바뀌었거나 색인이 없으면 그 종류를 처음부터 다시 만든다
    Args:
        c:     열린 연결
        kinds: 확인할 종류 (D: 원장 결정 · O: 미결 항목)
    """
    c.executescript(SCHEMA)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for kind in kinds:
        row = c.execute("SELECT value FROM index_meta WHERE name=?", (f"ver:{kind}",)).fetchone()
        if row and row[0] == A.version():
            continue
        c.execute("DELETE FROM term_index WHERE kind=?", (kind,))
        table, fn = ("decisions", index_decision) if kind == "D" else ("items", index_item)
        if table in tables:
            cur = c.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                fn(c, dict(zip(cols, r)))
        c.execute("INSERT OR REPLACE INTO index_meta VALUES (?,?)", (f"ver:{kind}", A.version()))


def _write(c: sqlite3.Connection, kind: str, ref: str, project_id: str, rows: list[tuple[str, str]]) -> None:
    """
    항목 하나의 색인 줄을 지우고 다시 쓴다
    Args:
        c:          열린 연결
        kind:       D | O
        ref:        결정 ID 또는 항목 ID
        project_id: 프로젝트 ID
        rows:       [(term, via)]
    """
    c.execute("DELETE FROM term_index WHERE kind=? AND ref=?", (kind, ref))
    c.executemany("INSERT OR IGNORE INTO term_index (term, kind, ref, project_id, via) VALUES (?,?,?,?,?)",
                  [(t, kind, ref, project_id, v) for t, v in rows])


def index_decision(c: sqlite3.Connection, d: dict) -> None:
    """
    원장 결정 하나를 색인 (추가·주제 수정 때)

    상태(승인·대체·반려)는 색인에 두지 않는다 — 찾은 뒤 원장에서 읽어 거른다.
    Args:
        c: 열린 연결 (원장과 같은 트랜잭션)
        d: 결정 레코드 (DB 행 또는 propose 입력 형식)
    """
    rows = [(f"d:{d['decision_id']}", "결정 ID")]
    if d.get("topic"):
        rows.append((f"t:{d['topic']}", "주제"))
    alts = _json(d.get("alternatives_rejected"), [])
    rows += _alias_terms(" / ".join([d.get("statement") or "", d.get("rationale") or "", *alts]))
    rows += [(f"m:{e['meeting_id']}", "근거 회의") for e in _json(d.get("evidence"), []) if e.get("meeting_id")]
    rows += [(f"w:{w}", "단어") for w in A.content_words(d.get("statement") or "")]
    _write(c, "D", d["decision_id"], d.get("project_id") or "", rows)


def index_item(c: sqlite3.Connection, it: dict) -> None:
    """
    회의 기록의 미결 하나를 색인 (미결이 아니면 색인에서 뺀다)
    Args:
        c:  열린 연결 (회의 기록과 같은 트랜잭션)
        it: 항목 레코드 (DB 행 형식)
    """
    if it.get("kind") != "open":
        c.execute("DELETE FROM term_index WHERE kind='O' AND ref=?", (it["item_id"],))
        return
    quotes = " / ".join(e.get("quote") or "" for e in _json(it.get("evidence"), []))
    rows = [(f"m:{it['meeting_id']}", "나온 회의")]
    if it.get("topic"):
        rows.append((f"t:{it['topic']}", "주제"))
    rows += _alias_terms(f"{it.get('statement') or ''} / {quotes}")
    if it.get("linked_to") and DECISION_ID.fullmatch(it["linked_to"]):
        rows.append((f"d:{it['linked_to']}", "이어진 결정"))
    rows += [(f"w:{w}", "단어") for w in A.content_words(it.get("statement") or "")]
    _write(c, "O", it["item_id"], it.get("project_id") or "", rows)


def drop(c: sqlite3.Connection, kind: str) -> None:
    """
    한 종류의 색인을 비운다 (시드 재적재·회의 기록 초기화 때)
    Args:
        c:    열린 연결
        kind: D | O
    """
    c.execute("DELETE FROM term_index WHERE kind=?", (kind,))


def lookup(c: sqlite3.Connection, terms: list[str], project_id: str | None) -> dict[tuple[str, str], list[str]]:
    """
    찾는 말과 같은 줄의 항목
    Args:
        c:          열린 연결
        terms:      찾는 말 목록 (t:… d:… m:… w:…)
        project_id: 이 프로젝트 항목만. 결정 ID(d:)로 직접 찾은 것은 프로젝트를 가리지 않는다. None이면 전체
    Returns:
        {(kind, ref): [일치 설명 …]}
    """
    out: dict[tuple[str, str], list[str]] = {}
    if not terms:
        return out
    q = f"SELECT term, kind, ref, project_id, via FROM term_index WHERE term IN ({','.join('?' * len(terms))})"
    for term, kind, ref, pid, via in c.execute(q, terms):
        if project_id and pid != project_id and not term.startswith("d:"):
            continue
        out.setdefault((kind, ref), []).append(f"{term[2:]}({via})" if term[0] != "w" else f"단어 '{term[2:]}'")
    return out


# ---------------------------------------------------------------- 회의 원문 단어 검색
class TranscriptIndex:
    """
    회의 원문 발언의 단어 색인 (BM25)

    원문 파일(r1_meetings/*.json)이 기준. sync가 바뀐 파일만 다시 색인하고 없어진 파일(파기)은 지운다.
    """

    K1, B = 1.2, 0.75     # BM25 표준값

    def __init__(self, db_path: Path | str):
        """
        색인 DB를 연다
        Args:
            db_path: SQLite 파일 경로 (원장과 같은 파일)
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        """
        요청 단위 연결 — 정상 종료면 커밋
        Returns:
            sqlite3.Connection
        """
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def seg_terms(text: str) -> dict[str, int]:
        """
        발언 하나의 색인 말 (내용어 + 별칭 주제 표식 #주제)
        Args:
            text: 발언
        Returns:
            {말: 횟수}
        """
        tf: dict[str, int] = {}
        for t in A.tokens(text):
            w = A.strip_particle(t)
            if w in A.content_words(w):
                tf[w] = tf.get(w, 0) + 1
        for h in A.match(text):
            tf["#" + h.topic] = tf.get("#" + h.topic, 0) + 1
        return tf

    def sync(self, data_dir: Path) -> int:
        """
        원문 파일과 색인을 맞춘다
        Args:
            data_dir: 시드 루트 (r1_meetings 하위)
        Returns:
            다시 색인한 회의 수
        """
        n = 0
        files = {f.stem: f for f in sorted((data_dir / "r1_meetings").glob("*.json"))}
        with self._conn() as c:
            row = c.execute("SELECT value FROM index_meta WHERE name='ver:R1'").fetchone()
            if not row or row[0] != A.version():
                c.execute("DELETE FROM r1_file")          # 별칭이 바뀌면 주제 표식이 달라져 전부 다시
            known = dict(c.execute("SELECT meeting_id, sig FROM r1_file").fetchall())
            for mid in set(known) - set(files):
                self._delete(c, mid)
            for mid, f in files.items():
                raw = f.read_bytes()
                sig = hashlib.sha1(raw).hexdigest()[:16]
                if known.get(mid) == sig:
                    continue
                self._delete(c, mid)
                m = json.loads(raw)
                for s in m.get("segments", []):
                    tf = self.seg_terms(s["text"])
                    c.execute("INSERT INTO r1_seg VALUES (?,?,?,?,?,?,?,?,?)",
                              (mid, s["seq"], m.get("project_id"), m.get("date"), s.get("ts"), s.get("speaker"),
                               s.get("role"), s["text"], sum(tf.values())))
                    c.executemany("INSERT INTO r1_term VALUES (?,?,?,?)",
                                  [(t, mid, s["seq"], v) for t, v in tf.items()])
                c.execute("INSERT OR REPLACE INTO r1_file VALUES (?,?)", (mid, sig))
                n += 1
            c.execute("INSERT OR REPLACE INTO index_meta VALUES ('ver:R1', ?)", (A.version(),))
        return n

    @staticmethod
    def _delete(c: sqlite3.Connection, meeting_id: str) -> int:
        """
        회의 하나의 색인 삭제 (연결 안에서)
        Args:
            c:          열린 연결
            meeting_id: 회의 ID
        Returns:
            지운 발언 수
        """
        n = c.execute("DELETE FROM r1_seg WHERE meeting_id=?", (meeting_id,)).rowcount
        c.execute("DELETE FROM r1_term WHERE meeting_id=?", (meeting_id,))
        c.execute("DELETE FROM r1_file WHERE meeting_id=?", (meeting_id,))
        return n

    def delete(self, meeting_id: str) -> int:
        """
        회의 하나의 발언 색인 삭제 (보존 기한이 지난 원문 파기용, 정책 SEC-02)
        Args:
            meeting_id: 회의 ID
        Returns:
            지운 발언 수
        """
        with self._conn() as c:
            return self._delete(c, meeting_id)

    def search(self, query: str, project_id: str | None = None, meeting_id: str | None = None,  # pylint: disable=too-many-locals
               k: int = 5) -> list[dict]:
        """
        질의와 낱말이 겹치는 발언을 BM25 순으로

        관련 기준: 질의와 같은 별칭 주제(#주제)를 가진 발언, 또는 내용어가 min(2, 질의 내용어 수)개 이상 겹치는 발언.
        한 낱말만 우연히 겹친 잡담은 버린다 (논의한 적 없는 주제에 아무 발언이나 붙는 것 방지).
        Args:
            query:      찾을 내용
            project_id: 프로젝트로 한정
            meeting_id: 회의로 한정
            k:          최대 결과 수
        Returns:
            발언 목록 (meeting_id, project_id, date, seq, ts, speaker, role, text, _score, matched)
        """
        words = A.content_words(query)
        tags = ["#" + h["topic"] for h in A.topics_of(query)]
        qterms = list(dict.fromkeys(words + tags))
        if not qterms:
            return []
        with self._conn() as c:
            n_docs, avg = c.execute("SELECT COUNT(*), AVG(n_terms) FROM r1_seg").fetchone()
            if not n_docs:
                return []
            avg = avg or 1.0
            df = dict(c.execute(f"SELECT term, COUNT(*) FROM r1_term WHERE term IN ({','.join('?' * len(qterms))}) "
                                "GROUP BY term", qterms).fetchall())
            q = (f"SELECT t.term, t.tf, s.meeting_id, s.seq, s.project_id, s.date, s.ts, s.speaker, s.role, s.text, "
                 f"s.n_terms FROM r1_term t JOIN r1_seg s ON s.meeting_id=t.meeting_id AND s.seq=t.seq "
                 f"WHERE t.term IN ({','.join('?' * len(qterms))})")
            args: list = list(qterms)
            if project_id:
                q, args = q + " AND s.project_id=?", [*args, project_id]
            if meeting_id:
                q, args = q + " AND s.meeting_id=?", [*args, meeting_id]
            docs: dict[tuple[str, int], dict] = {}
            for term, tf, mid, seq, pid, dt, ts, spk, role, text, dl in c.execute(q, args):
                d = docs.setdefault((mid, seq), {"meeting_id": mid, "project_id": pid, "date": dt, "seq": seq, "ts": ts,
                                                 "speaker": spk, "role": role, "text": text, "_score": 0.0,
                                                 "matched": []})
                idf = math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1)
                d["_score"] += idf * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * dl / avg))
                d["matched"].append(term)
        need = min(2, len(words))
        keep = [d for d in docs.values()
                if any(m in tags for m in d["matched"]) or (need and sum(m in words for m in d["matched"]) >= need)]
        for d in keep:
            d["_score"] = round(d["_score"], 3)
        return sorted(keep, key=lambda d: (-d["_score"], d["date"] or "", d["seq"]))[:k]


# ---------------------------------------------------------------- 후보 꺼내기 (원장·회의 기록에서 상태를 읽어 거른다)
def _decision_cand(d: dict, records) -> dict:
    """
    원장 결정을 후보 형식으로
    Args:
        d:       LedgerStore.get_decision 결과
        records: 회의 기록 (근거 회의 날짜를 찾는다)
    Returns:
        후보 dict (kind=decision, decision_id, project_id, topic, status, active, statement, date)
    """
    ev = d.get("evidence") or []
    m = records.meeting(ev[0]["meeting_id"]) if ev and ev[0].get("meeting_id") else None
    return {"kind": "decision", "decision_id": d["decision_id"], "project_id": d["project_id"], "topic": d["topic"],
            "status": d["status"], "active": d["is_active"], "statement": d["statement"],
            "date": (m or {}).get("date") or d.get("effective_from") or ""}


def _usable(c: dict) -> bool:
    """
    LLM에 보여 줄 후보인가 (결정: 유효 또는 승인 대기 · 미결: 열림 또는 결정으로 이어짐)

    대체·반려된 결정과 사람이 닫은 미결은 뺀다 — 예전 벡터 검색(유효 + 승인 대기만)과 같은 범위.
    Args:
        c: 후보 dict
    Returns:
        보여 줄지 여부
    """
    if c["kind"] == "decision":
        return c["active"] or c["status"] == "proposed"
    return c.get("state") in ("open", "linked")


def project_records(store, records, project_id: str) -> list[dict]:
    """
    프로젝트 기록 전체 (전체 읽기 대비책용): 유효·승인 대기 결정 + 열린·이어진 미결
    Args:
        store:      LedgerStore
        records:    MeetingRecords
        project_id: 프로젝트 ID
    Returns:
        후보 목록 (결정 먼저, 그다음 미결 날짜순)
    """
    decs = [_decision_cand(d, records) for d in store.decisions(project_id=project_id)]
    opens = [{**o, "kind": "open"} for o in records.open_items(project_id, include_linked=True)]
    return [c for c in decs + opens if _usable(c)]


def find_candidates(store, records, project_id: str, topics: list[str], decision_ids: list[str],  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
                    meeting_ids: list[str], words: list[str], period: tuple[str, str] | None = None) -> dict:
    """
    색인으로 좁힌 후보 — 주제·ID로 먼저, 하나도 없으면 단어로

    범위: 질문 프로젝트(결정 ID로 직접 찾은 것은 예외), 기간(주어지면, 거른 결과가 비면 기간 조건을 뺀다).
    회의 ID만 있고 주제가 없으면 그 회의에서 나온 결정·미결 전부, 주제와 회의가 함께 있으면 둘 다 맞는 것.
    Args:
        store:        LedgerStore
        records:      MeetingRecords
        project_id:   질문 대상 프로젝트
        topics:       별칭·LLM이 가리킨 주제 키
        decision_ids: 질문에 적힌 결정 ID
        meeting_ids:  질문에 적혔거나 입력창에서 고른 회의 ID
        words:        질문의 내용어 (둘째 단계)
        period:       (시작일, 종료일) — 질문에 기간 표현이 있을 때
    Returns:
        {"candidates": 후보(via 포함), "tier": "주제·ID" | "단어" | "", "period_dropped": 기간 조건을 뺐는지}
    """
    with sqlite3.connect(store.db_path) as c:
        found = lookup(c, [f"t:{t}" for t in topics] + [f"d:{i}" for i in decision_ids], project_id)
        if meeting_ids:
            in_meeting = lookup(c, [f"m:{m}" for m in meeting_ids], project_id)
            # 주제·ID와 회의가 함께 있으면 교집합, 회의만 있으면 그 회의 항목 전부
            found = ({k: v + in_meeting[k] for k, v in found.items() if k in in_meeting} if found else in_meeting) \
                or found
        tier = "주제·ID" if found else ""
        if not found and words:
            hits = lookup(c, [f"w:{w}" for w in words], project_id)
            need = min(2, len(words))
            found = {k: v for k, v in hits.items() if len(v) >= need}
            tier = "단어" if found else ""
    cands = []
    for (kind, ref), via in found.items():
        if kind == "D":
            d = store.get_decision(ref)
            c = _decision_cand(d, records) if d else None
        else:
            it = records.item(ref)
            c = {**it, "kind": "open"} if it else None
        if c and _usable(c):
            cands.append({**c, "via": list(dict.fromkeys(via))})
    dropped = False
    if period:
        inside = [x for x in cands if period[0] <= (x.get("date") or "") <= period[1]]
        dropped = bool(cands) and not inside
        cands = inside or cands
    cands.sort(key=lambda x: (x["kind"] != "decision", x.get("date") or "", x.get("decision_id") or x.get("item_id")))
    return {"candidates": cands, "tier": tier, "period_dropped": dropped}
