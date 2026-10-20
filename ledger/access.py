"""사용자·권한·요청·알림·대화방 (SQLite `store/access.db`).

권한 규칙
- 회의 원문·결정 열람: 기본은 그 회의 참석자만. 팀장은 전부.
- 관리자 콘솔에서 회의별로 역할(인턴·팀원·담당자)에 열람을 열어 줄 수 있다.
- 권한 밖의 승인된 결정·회의는 '열람 요청' → 팀장 승인 → (이 사람만 / 같은 역할 전체 / 7일) 범위로 허용.
- 결정·의견 승인과 반려는 팀장만. 결과는 요청자와 승인자 모두에게 알림으로 남긴다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "project"
DB = ROOT / "store" / "access.db"
ROLE_RANK = {"인턴": 0, "팀원": 1, "담당자": 2, "팀장": 3}
SCOPE_KO = {"user": "이 사람만", "role": "같은 역할 전체", "all": "전체"}
DURATION_KO = {"once": "1회", "7d": "7일", "project": "프로젝트 기간 동안", "forever": "무기한", "until": "기한 지정"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_acl (meeting_id TEXT, role TEXT, granted_by TEXT, created TEXT,
                                        PRIMARY KEY (meeting_id, role));
CREATE TABLE IF NOT EXISTS access_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT, target TEXT,
    reason TEXT, status TEXT DEFAULT 'pending', decided_by TEXT, scope TEXT, expires TEXT, created TEXT, decided TEXT,
    duration TEXT, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS proposals (decision_id TEXT PRIMARY KEY, requested_by TEXT, source TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, text TEXT,
    kind TEXT, link TEXT, created TEXT, read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS answer_cache (key TEXT PRIMARY KEY, fingerprint TEXT, final TEXT, payload TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, user_id TEXT, title TEXT, project_key TEXT,
    meeting_id TEXT, updated TEXT);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AccessStore:
    def __init__(self, db: Path = DB):
        db.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(db, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for col in ("duration TEXT", "used INTEGER DEFAULT 0"):      # 이전 버전 DB 호환
            try:
                self.conn.execute(f"ALTER TABLE access_requests ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.users = {u["user_id"]: u for u in json.loads((DATA / "users.json").read_text())["users"]}
        self.projects = json.loads((DATA / "projects.json").read_text())["projects"]

    # ---------------------------------------------------------------- 사용자
    def user(self, user_id: str) -> dict:
        return self.users.get(user_id) or self.users["yhr"]

    def leads(self) -> list[str]:
        return [u for u, v in self.users.items() if v["role"] == "팀장"]

    def is_lead(self, user_id: str) -> bool:
        return self.user(user_id)["role"] == "팀장"

    def key_of(self, project_id: str) -> str:
        return next((p["key"] for p in self.projects if p["project_id"] == project_id), "OPS")

    # ---------------------------------------------------------------- 권한 판정
    def _grant_valid(self, r) -> bool:
        """승인된 열람의 기간 확인: 1회(아직 안 씀), 7일(만료 전), 프로젝트 기간(프로젝트 진행 중), 무기한."""
        d = r["duration"] or ("7d" if r["expires"] else "forever")
        if d == "once":
            return not r["used"]
        if d in ("7d", "until"):
            return bool(r["expires"]) and r["expires"] >= now()
        if d == "project":
            key = r["target"].split("-")[0]
            return any(p["key"] == key and p["status"] == "진행" for p in self.projects)
        return True

    def _approved_request(self, user_id: str, target: str, consume: bool = False) -> bool:
        u = self.user(user_id)
        for r in self.conn.execute("SELECT * FROM access_requests WHERE target=? AND status='approved'", (target,)):
            if not self._grant_valid(r):
                continue
            scope = r["scope"] if r["scope"] in SCOPE_KO else "user"
            if scope == "all" or (scope == "role" and self.user(r["requester"])["role"] == u["role"]) \
                    or (scope == "user" and r["requester"] == user_id):
                if consume and r["duration"] == "once":
                    with self.conn:
                        self.conn.execute("UPDATE access_requests SET used=1 WHERE id=?", (r["id"],))
                return True
        return False

    def can_view_meeting(self, user_id: str, meeting: dict | None, consume: bool = False) -> bool:
        """consume=True는 실제로 여는 순간(회의 정리 실행)에만. 목록 표시는 1회 열람권을 쓰지 않는다."""
        if meeting is None:
            return False
        u = self.user(user_id)
        if u["role"] == "팀장" or u["name"] in (meeting.get("attendees") or []):
            return True
        if self.conn.execute("SELECT 1 FROM meeting_acl WHERE meeting_id=? AND role=?",
                             (meeting["meeting_id"], u["role"])).fetchone():
            return True
        return self._approved_request(user_id, meeting["meeting_id"], consume)

    def can_view_decision(self, user_id: str, decision: dict, meetings: dict[str, dict], consume: bool = False) -> bool:
        """근거 회의 중 하나라도 볼 수 있으면 열람 가능. 원문이 파기된 과거 결정은 담당자 이상."""
        u = self.user(user_id)
        if u["role"] == "팀장" or self._approved_request(user_id, decision["decision_id"], consume):
            return True
        ev_meetings = {e["meeting_id"] for e in decision.get("evidence", [])}
        alive = [meetings[m] for m in ev_meetings if m in meetings]
        if not alive:
            return ROLE_RANK[u["role"]] >= ROLE_RANK["담당자"]
        return any(self.can_view_meeting(user_id, m) for m in alive)

    # ---------------------------------------------------------------- 관리자: 회의 접근 권한
    def acl(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.conn.execute("SELECT meeting_id, role FROM meeting_acl"):
            out.setdefault(r["meeting_id"], []).append(r["role"])
        return out

    def set_acl(self, meeting_id: str, role: str, on: bool, by: str) -> None:
        with self.conn:
            if on:
                self.conn.execute("INSERT OR REPLACE INTO meeting_acl VALUES (?,?,?,?)", (meeting_id, role, by, now()))
            else:
                self.conn.execute("DELETE FROM meeting_acl WHERE meeting_id=? AND role=?", (meeting_id, role))

    # ---------------------------------------------------------------- 열람 요청
    def request_access(self, requester: str, target: str, reason: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO access_requests (requester, target, reason, created) VALUES (?,?,?,?)",
                                    (requester, target, reason, now()))
        name = self.user(requester)["name"]
        for lead in self.leads():
            self.notify(lead, f"{name}님이 {target} 열람을 요청했습니다", "request", "admin")
        self.notify(requester, f"{target} 열람 요청을 보냈습니다 (팀장 승인 대기)", "info")
        return cur.lastrowid

    def requests(self, status: str | None = "pending") -> list[dict]:
        sql = "SELECT * FROM access_requests" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
        rows = [dict(r) for r in self.conn.execute(sql, (status,) if status else ())]
        for r in rows:
            u = self.user(r["requester"])
            r["requester_name"], r["requester_role"] = u["name"], u["role"]
        return rows

    def decide_request(self, req_id: int, by: str, approve: bool, scope: str = "user", duration: str = "once",
                       until: str = "") -> dict:
        """열람 승인. 범위(이 사람만 / 같은 역할 전체 / 전체)와 기간(1회 / 프로젝트 기간 / 무기한 / 직접 입력한 날짜까지)."""
        r = self.conn.execute("SELECT * FROM access_requests WHERE id=?", (req_id,)).fetchone()
        if r is None:
            raise KeyError(req_id)
        scope = scope if scope in SCOPE_KO else "user"
        duration = duration if duration in DURATION_KO else "once"
        if duration == "until" and not until:
            duration = "once"
        expires = ((datetime.now() + timedelta(days=7)).isoformat(timespec="seconds") if duration == "7d"
                   else f"{until[:10]}T23:59:59" if duration == "until" else None)
        with self.conn:
            self.conn.execute("UPDATE access_requests SET status=?, decided_by=?, scope=?, duration=?, expires=?, decided=? WHERE id=?",
                              ("approved" if approve else "rejected", by, scope, duration, expires, now(), req_id))
        label = f"{SCOPE_KO[scope]} · " + (f"{until[:10]}까지" if duration == "until" else DURATION_KO[duration])
        who = self.user(r["requester"])["name"]
        if approve:
            self.notify(r["requester"], f"{r['target']} 열람 요청이 승인되었습니다 ({label})", "approved")
            self.notify(by, f"{who}님의 {r['target']} 열람을 승인했습니다 ({label})", "done")
        else:
            self.notify(r["requester"], f"{r['target']} 열람 요청이 반려되었습니다", "rejected")
            self.notify(by, f"{who}님의 {r['target']} 열람 요청을 반려했습니다", "done")
        return {"id": req_id, "status": "approved" if approve else "rejected", "scope": scope, "duration": duration}

    # ---------------------------------------------------------------- 결정·의견 승인 요청
    def record_proposal(self, decision_id: str, requested_by: str, source: str) -> None:
        exists = self.conn.execute("SELECT 1 FROM proposals WHERE decision_id=?", (decision_id,)).fetchone()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO proposals VALUES (?,?,?,?)", (decision_id, requested_by, source, now()))
        if not exists:
            name = self.user(requested_by)["name"]
            for lead in self.leads():
                if lead != requested_by:
                    self.notify(lead, f"{decision_id} 승인 요청 ({name}, {source})", "request", "admin")

    def proposer(self, decision_id: str) -> str | None:
        r = self.conn.execute("SELECT requested_by FROM proposals WHERE decision_id=?", (decision_id,)).fetchone()
        return r["requested_by"] if r else None

    def proposal_sources(self) -> dict[str, dict]:
        return {r["decision_id"]: dict(r) for r in self.conn.execute("SELECT * FROM proposals")}

    def after_decision(self, decision_id: str, by: str, approved: bool, superseded: list[str], reason: str = "") -> None:
        who = self.proposer(decision_id)
        if approved:
            extra = f" — 대체: {', '.join(superseded)}" if superseded else ""
            self.notify(by, f"{decision_id} 승인했습니다{extra}", "done")
            if who and who != by:
                self.notify(who, f"{decision_id} 승인 요청이 승인되었습니다 ({self.user(by)['name']})", "approved")
        else:
            self.notify(by, f"{decision_id} 반려했습니다", "done")
            if who and who != by:
                self.notify(who, f"{decision_id} 승인 요청이 반려되었습니다{' — ' + reason if reason else ''}", "rejected")

    # ---------------------------------------------------------------- 알림
    def notify(self, user_id: str, text: str, kind: str = "info", link: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT INTO notifications (user_id, text, kind, link, created) VALUES (?,?,?,?,?)",
                              (user_id, text, kind, link, now()))

    def notifications(self, user_id: str, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]

    def mark_read(self, user_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user_id,))

    # ---------------------------------------------------------------- 답변 재사용
    def cache_get(self, key: str, fingerprint: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM answer_cache WHERE key=?", (key,)).fetchone()
        if r is None or r["fingerprint"] != fingerprint:
            return None            # 처음이거나, 원장·정책·문서·원문 중 무엇이 바뀌었으면 새로 만든다
        return {"final": r["final"], "created": r["created"], **json.loads(r["payload"])}

    def cache_clear(self) -> None:
        """모델을 바꾸면 이전 모델로 만든 답이 섞이지 않게 저장 답변을 비운다."""
        with self.conn:
            self.conn.execute("DELETE FROM answer_cache")

    def cache_put(self, key: str, fingerprint: str, final: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO answer_cache VALUES (?,?,?,?,?)",
                              (key, fingerprint, final, json.dumps(payload, ensure_ascii=False, default=str), now()))

    # ---------------------------------------------------------------- 대화방
    def touch_thread(self, thread_id: str, user_id: str, title: str, project_key: str | None, meeting_id: str | None) -> None:
        r = self.conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        with self.conn:
            if r is None:
                self.conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)",
                                  (thread_id, user_id, title[:40], project_key, meeting_id, now()))
            else:
                self.conn.execute("UPDATE threads SET updated=?, project_key=COALESCE(project_key, ?), "
                                  "meeting_id=COALESCE(meeting_id, ?) WHERE thread_id=?",
                                  (now(), project_key, meeting_id, thread_id))

    def delete_thread(self, thread_id: str, user_id: str) -> bool:
        """본인 대화방만 지운다 (대화 내용은 서버가 session.db에서 함께 지움)."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM threads WHERE thread_id=? AND user_id=?", (thread_id, user_id))
        return cur.rowcount > 0

    def threads(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM threads WHERE user_id=? ORDER BY updated DESC", (user_id,))]

    def reset(self) -> None:
        with self.conn:
            for t in ("meeting_acl", "access_requests", "proposals", "notifications", "threads", "answer_cache"):
                self.conn.execute(f"DELETE FROM {t}")
