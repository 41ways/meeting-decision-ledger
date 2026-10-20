"""회의 기록 — 회의 목록·회의에서 나온 항목·사안·이력 (docs/설계_재구성안.md §2).

원장(decisions)은 승인된 결정만 담는다. 그 밖에 회의에서 나온 것(결정 후보·미결·할 일)과
회의 정보(날짜·참석자), 상태 변화 이력은 여기에 남긴다. 같은 ledger.db 파일을 쓴다.

- meetings: 원문을 파기해도 남는다 (언제·누가 참석한 회의인가는 발언 내용이 아니라 기록이다)
- items:    회의 정리 결과. 고유 키(회의 ID:종류:발언 번호)로 덮어써서 다시 정리해도 두 번 생기지 않는다
- threads:  같은 프로젝트·주제의 항목을 회의를 가로질러 묶은 '사안'. 미결 → 결정 연결이 여기서 일어난다
- events:   모든 상태 변화 (누가, 언제, 무엇을, 왜)

상태(확정·대체)는 원장에만 둔다. 항목은 결정 ID로 원장을 가리키기만 한다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .store import DATA, DEFAULT_DB, project_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY,
    project_id TEXT, domain TEXT, date TEXT, title TEXT,
    attendees TEXT,                  -- JSON [{name, role}]
    transcript_deleted INTEGER NOT NULL DEFAULT 0,
    summary TEXT,                    -- JSON {topic, lines:[{text, seqs}], direction} — 원문과 같은 보존 기한
    processed_at TEXT, pipeline_version TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    project_id TEXT, topic TEXT, title TEXT, created TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,        -- 회의 ID:종류:발언 번호
    meeting_id TEXT NOT NULL, project_id TEXT, date TEXT,
    kind TEXT NOT NULL,              -- decision | open | action
    seq INTEGER, statement TEXT,
    slots TEXT,                      -- JSON {target, change, scope, condition}
    topic TEXT, thread_id TEXT,
    label TEXT,                      -- 확인 필요 | 승인 대기 | 미결 | 할 일
    review TEXT,                     -- JSON (판정·해결 제안)
    evidence TEXT,                   -- JSON [{meeting_id, seq, speaker, quote}]
    decision_id TEXT,                -- 원장에 올린 결정
    state TEXT,                      -- open | linked | closed (미결) / proposed | held (결정 후보) / open (할 일)
    linked_to TEXT,                  -- 미결이 이어진 결정 항목·결정 ID
    owner TEXT, due TEXT,
    created TEXT, updated TEXT
);
CREATE INDEX IF NOT EXISTS items_thread ON items(thread_id);
CREATE INDEX IF NOT EXISTS items_meeting ON items(meeting_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, actor TEXT, kind TEXT NOT NULL, target TEXT, detail TEXT
);
"""

JSON_COLS = ("attendees", "summary", "slots", "review", "evidence", "detail")


def _now() -> str:
    """현재 시각 (초 단위 ISO 문자열)"""
    return datetime.now().isoformat(timespec="seconds")


def thread_id_for(project_id: str, topic: str, item_id: str) -> str:
    """
    사안 ID — 같은 프로젝트·주제면 같은 사안
    Args:
        project_id: 프로젝트 ID
        topic:      주제 키 (빈 문자열이면 묶지 않는다)
        item_id:    주제가 없을 때 쓰는 항목 ID
    Returns:
        사안 ID (예: T-OPS-cache, 주제 없으면 T-OPS-M04:open:10)
    """
    # 주제를 모르면 억지로 묶지 않는다 — 엉뚱한 미결이 결정에 이어지는 것을 막는다
    return f"T-{project_key(project_id)}-{topic}" if topic else f"T-{item_id}"


class MeetingRecords:
    """
    회의 목록·항목·사안·이력 저장소 (ledger.db 공유)
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB):
        """
        DB를 열고 스키마 생성
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
            sqlite3.Connection (row_factory=sqlite3.Row)
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        """
        행을 dict로 (JSON 컬럼은 풀어서)
        Args:
            row: 조회 결과 행
        Returns:
            dict 또는 None
        """
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLS:
            if k in d and isinstance(d[k], str) and d[k]:
                d[k] = json.loads(d[k])
        return d

    def reset(self) -> None:
        """
        회의 기록을 비운다 (시연·평가 초기화용 — 시드 재적재와 함께만 쓴다)
        """
        with self._conn() as c:
            for table in ("items", "threads", "events"):
                c.execute(f"DELETE FROM {table}")
            c.execute("UPDATE meetings SET summary=NULL, processed_at=NULL, pipeline_version=NULL")

    # ---------------- 이력 ----------------
    def log(self, actor: str, kind: str, target: str, detail: dict | None = None) -> None:
        """
        상태 변화 기록
        Args:
            actor:  누가 (사용자 이름·ID 또는 'agent')
            kind:   무엇을 (meeting_processed, item_linked, item_closed, decision_proposed …)
            target: 대상 ID
            detail: 추가 정보
        """
        with self._conn() as c:
            c.execute("INSERT INTO events (at, actor, kind, target, detail) VALUES (?,?,?,?,?)",
                      (_now(), actor, kind, target, json.dumps(detail or {}, ensure_ascii=False)))

    def events(self, target: str | None = None, kind: str | None = None) -> list[dict]:
        """
        이력 조회 (오래된 순)
        Args:
            target: 대상 ID로 한정
            kind:   종류로 한정
        Returns:
            이력 목록
        """
        q, args = "SELECT * FROM events WHERE 1=1", []
        if target:
            q, args = q + " AND target=?", [*args, target]
        if kind:
            q, args = q + " AND kind=?", [*args, kind]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q + " ORDER BY id", args)]

    # ---------------- 회의 목록 ----------------
    def sync_meetings(self, data_dir: Path = DATA) -> int:
        """
        원문 파일의 머리 정보(날짜·제목·참석자)를 회의 목록에 반영

        파일이 없어진 회의(파기)는 지우지 않고 transcript_deleted만 표시한다 — 회의 정보는 계속 남는다.
        Args:
            data_dir: 시드 루트 (r1_meetings 하위)
        Returns:
            반영한 회의 수
        """
        users = json.loads((data_dir / "users.json").read_text(encoding="utf-8"))["users"] \
            if (data_dir / "users.json").exists() else []
        roles = {u["name"]: u.get("title") or u["role"] for u in users}
        seen = set()
        with self._conn() as c:
            for f in sorted((data_dir / "r1_meetings").glob("*.json")):
                m = json.loads(f.read_text(encoding="utf-8"))
                # 역할은 발언에 붙은 역할이 가장 정확하다 (없으면 사용자 목록)
                spoke = {s["speaker"]: s.get("role") for s in m.get("segments", [])}
                att = [{"name": n, "role": spoke.get(n) or roles.get(n, "")} for n in m.get("attendees", [])]
                c.execute("""INSERT INTO meetings
                             (meeting_id, project_id, domain, date, title, attendees, transcript_deleted)
                             VALUES (?,?,?,?,?,?,0)
                             ON CONFLICT(meeting_id) DO UPDATE SET project_id=excluded.project_id, domain=excluded.domain,
                             date=excluded.date, title=excluded.title, attendees=excluded.attendees,
                             transcript_deleted=0""",
                          (m["meeting_id"], m.get("project_id"), m.get("domain", "meeting"), m.get("date"),
                           m.get("title"), json.dumps(att, ensure_ascii=False)))
                seen.add(m["meeting_id"])
            for r in c.execute("SELECT meeting_id FROM meetings").fetchall():
                if r["meeting_id"] not in seen:
                    c.execute("UPDATE meetings SET transcript_deleted=1 WHERE meeting_id=?", (r["meeting_id"],))
        return len(seen)

    def meeting(self, meeting_id: str) -> dict | None:
        """
        회의 하나
        Args:
            meeting_id: 회의 ID
        Returns:
            회의 정보 (attendees, summary 포함) 또는 None
        """
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone())

    def meetings(self, project_id: str | None = None) -> list[dict]:
        """
        회의 목록 (날짜순)
        Args:
            project_id: 프로젝트로 한정
        Returns:
            회의 목록
        """
        q, args = "SELECT * FROM meetings", []
        if project_id:
            q, args = q + " WHERE project_id=?", [project_id]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q + " ORDER BY date, meeting_id", args)]

    # ---------------- 회의 정리 결과 ----------------
    def save_meeting(self, meeting_id: str, summary: dict | None, items: list[dict],
                     pipeline_version: str, actor: str = "agent") -> dict:
        """
        회의 정리 결과를 저장하고 사안에 묶은 뒤, 이전 회의의 열린 미결을 이번 결정에 잇는다

        항목은 고유 키로 덮어쓴다 — 같은 회의를 다시 정리해도 두 번 생기지 않는다.
        미결 연결은 같은 사안(프로젝트·주제)에서만, 이 회의보다 앞선 회의의 열린 미결만 대상으로 한다.
        Args:
            meeting_id:       회의 ID
            summary:          요약 {topic, lines:[{text, seqs}], direction} (없으면 None)
            items:            항목 목록 (kind, seq, statement, slots, topic, label, review, evidence,
                              decision_id, state, owner, due)
            pipeline_version: 정리에 쓴 파이프라인 버전
            actor:            정리를 실행한 사람·에이전트
        Returns:
            {"saved": 저장 건수, "linked": [{"open": 미결 항목 ID, "to": 결정 항목 ID}]}
        """
        m = self.meeting(meeting_id) or {}
        pid, date = m.get("project_id") or "PRJ-OPSAI", m.get("date") or ""
        now, linked = _now(), []
        with self._conn() as c:
            c.execute("UPDATE meetings SET summary=?, processed_at=?, pipeline_version=? WHERE meeting_id=?",
                      (json.dumps(summary, ensure_ascii=False) if summary else None, now, pipeline_version, meeting_id))
            for it in items:
                iid = f"{meeting_id}:{it['kind']}:{it.get('seq')}"
                tid = thread_id_for(pid, it.get("topic") or "", iid)
                if it.get("topic"):
                    c.execute("INSERT OR IGNORE INTO threads (thread_id, project_id, topic, title, created, updated) "
                              "VALUES (?,?,?,?,?,?)", (tid, pid, it["topic"], it.get("statement", ""), now, now))
                    c.execute("UPDATE threads SET updated=? WHERE thread_id=?", (now, tid))
                old = c.execute("SELECT state, linked_to, created FROM items WHERE item_id=?", (iid,)).fetchone()
                # 다시 정리할 때 사람이 닫았거나 이어 둔 미결 상태는 지키고 내용만 갱신한다
                state = old["state"] if old and old["state"] in ("closed", "linked") else it.get("state")
                c.execute("""INSERT OR REPLACE INTO items
                             (item_id, meeting_id, project_id, date, kind, seq, statement, slots,
                             topic, thread_id, label, review, evidence, decision_id, state, linked_to, owner, due, created, updated)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (iid, meeting_id, pid, date, it["kind"], it.get("seq"), it.get("statement"),
                           json.dumps(it.get("slots") or {}, ensure_ascii=False), it.get("topic") or "", tid,
                           it.get("label"), json.dumps(it.get("review") or {}, ensure_ascii=False),
                           json.dumps(it.get("evidence") or [], ensure_ascii=False), it.get("decision_id"), state,
                           old["linked_to"] if old else None, it.get("owner"), it.get("due"),
                           old["created"] if old else now, now))
            # 이번 회의의 결정이 같은 사안의 앞선 열린 미결을 해소한다
            for it in items:
                if it["kind"] != "decision" or not it.get("topic"):
                    continue
                did = f"{meeting_id}:decision:{it.get('seq')}"
                tid = thread_id_for(pid, it["topic"], did)
                rows = c.execute("SELECT item_id FROM items WHERE thread_id=? AND kind='open' AND state='open' "
                                 "AND meeting_id<>? AND date<=?", (tid, meeting_id, date)).fetchall()
                for r in rows:
                    c.execute("UPDATE items SET state='linked', linked_to=?, updated=? WHERE item_id=?",
                              (it.get("decision_id") or did, now, r["item_id"]))
                    linked.append({"open": r["item_id"], "to": it.get("decision_id") or did})
        self.log(actor, "meeting_processed", meeting_id, {"items": len(items), "pipeline": pipeline_version})
        for x in linked:
            self.log(actor, "item_linked", x["open"], {"to": x["to"]})
        return {"saved": len(items), "linked": linked}

    def items(self, meeting_id: str | None = None, kind: str | None = None, thread_id: str | None = None,
              project_id: str | None = None, state: str | None = None) -> list[dict]:
        """
        항목 조회 (날짜·발언 순)
        Args:
            meeting_id: 회의로 한정
            kind:       종류로 한정 (decision | open | action)
            thread_id:  사안으로 한정
            project_id: 프로젝트로 한정
            state:      상태로 한정
        Returns:
            항목 목록
        """
        q, args = "SELECT * FROM items WHERE 1=1", []
        for col, v in (("meeting_id", meeting_id), ("kind", kind), ("thread_id", thread_id),
                       ("project_id", project_id), ("state", state)):
            if v:
                q, args = q + f" AND {col}=?", [*args, v]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q + " ORDER BY date, meeting_id, seq", args)]

    def item(self, item_id: str) -> dict | None:
        """
        항목 하나
        Args:
            item_id: 항목 ID
        Returns:
            항목 또는 None
        """
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone())

    def items_for_decision(self, decision_id: str) -> list[dict]:
        """
        원장 결정에 연결된 항목 (결정 후보와 그 결정으로 이어진 미결)
        Args:
            decision_id: 결정 ID
        Returns:
            항목 목록
        """
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM items WHERE decision_id=? OR linked_to=? ORDER BY date, seq",
                (decision_id, decision_id))]

    def link_decision(self, item_id: str, decision_id: str, actor: str = "agent") -> None:
        """
        결정 후보 항목에 원장 결정 ID를 잇고, 이 항목에 이어졌던 미결도 결정 ID를 가리키게 한다
        Args:
            item_id:     결정 후보 항목 ID
            decision_id: 원장 결정 ID
            actor:       실행한 사람·에이전트
        """
        with self._conn() as c:
            c.execute("UPDATE items SET decision_id=?, state='proposed', updated=? WHERE item_id=?",
                      (decision_id, _now(), item_id))
            c.execute("UPDATE items SET linked_to=? WHERE linked_to=?", (decision_id, item_id))
        self.log(actor, "decision_proposed", decision_id, {"item": item_id})

    def close_open(self, item_id: str, actor: str, reason: str) -> dict:
        """
        미결을 사람이 닫는다 (회의 밖에서 정해졌거나 하지 않기로 함)
        Args:
            item_id: 미결 항목 ID
            actor:   닫은 사람
            reason:  사유
        Returns:
            갱신된 항목
        Raises:
            ValueError: 미결이 아니거나 이미 닫힘
        """
        it = self.item(item_id)
        if not it or it["kind"] != "open" or it["state"] != "open":
            raise ValueError(f"열린 미결이 아닙니다: {item_id}")
        with self._conn() as c:
            c.execute("UPDATE items SET state='closed', updated=? WHERE item_id=?", (_now(), item_id))
        self.log(actor, "item_closed", item_id, {"reason": reason})
        return self.item(item_id)

    def thread(self, thread_id: str) -> list[dict]:
        """
        사안의 흐름 (회의 날짜순 항목)
        Args:
            thread_id: 사안 ID
        Returns:
            항목 목록
        """
        return self.items(thread_id=thread_id)

    def thread_of_topic(self, project_id: str, topic: str) -> list[dict]:
        """
        프로젝트·주제로 사안의 흐름 조회
        Args:
            project_id: 프로젝트 ID
            topic:      주제 키
        Returns:
            항목 목록 (주제가 없으면 빈 목록)
        """
        return self.thread(thread_id_for(project_id, topic, "")) if topic else []

    def open_items(self, project_id: str | None = None, include_linked: bool = False) -> list[dict]:
        """
        미결 목록
        Args:
            project_id:     프로젝트로 한정
            include_linked: 결정으로 이어진 미결도 포함
        Returns:
            미결 항목 목록
        """
        rows = self.items(kind="open", project_id=project_id)
        return [r for r in rows if r["state"] == "open" or (include_linked and r["state"] == "linked")]

    def forget_meeting_details(self, meeting_id: str, actor: str = "retention") -> dict:
        """
        보존 기한이 지난 회의의 논의 내용을 지운다 (회의 정보·결정·할 일은 남긴다)

        요약과 미결의 내용·인용은 원문 요약이라 원문과 함께 지운다.
        미결은 '주제·회의·담당'만 남은 최소 항목으로 줄인다.
        Args:
            meeting_id: 회의 ID
            actor:      실행 주체
        Returns:
            {"summary_cleared": bool, "open_trimmed": 줄인 미결 수}
        """
        with self._conn() as c:
            had = c.execute("SELECT summary FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
            c.execute("UPDATE meetings SET summary=NULL, transcript_deleted=1 WHERE meeting_id=?", (meeting_id,))
            n = c.execute("UPDATE items SET statement='(원문 파기 — 내용 삭제)', evidence='[]', slots='{}' "
                          "WHERE meeting_id=? AND kind='open'", (meeting_id,)).rowcount
        self.log(actor, "meeting_details_forgotten", meeting_id, {"open_trimmed": n})
        return {"summary_cleared": bool(had and had["summary"]), "open_trimmed": n}
