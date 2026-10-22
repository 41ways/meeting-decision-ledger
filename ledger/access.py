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
CREATE TABLE IF NOT EXISTS thread_refs (thread_id TEXT PRIMARY KEY, refs TEXT);
"""


def now() -> str:
    """
    현재 시각을 초 단위 ISO 문자열로 반환

    문자열 비교로 만료 판정을 하므로(expires >= now()) 형식을 고정.
    Returns:
        "YYYY-MM-DDTHH:MM:SS" 형식 문자열
    """
    return datetime.now().isoformat(timespec="seconds")


class AccessStore:
    """
    사용자·권한·열람 요청·승인 요청·알림·답변 캐시·대화방을 다루는 저장소

    Attributes:
        conn:  access.db 연결 (행을 이름으로 꺼내도록 sqlite3.Row)
        users:  user_id → 사용자 정보 (data/project/users.json)
        projects:  프로젝트 목록 (data/project/projects.json)
    """

    def __init__(self, db: Path = DB):
        """
        DB를 열어 스키마를 만들고 사용자·프로젝트 정보를 메모리에 적재
        Args:
            db:  access.db 경로
        """
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
        """
        사용자 정보 조회

        알 수 없는 ID면 기본 사용자(yhr)로 대체해 KeyError 없이 진행.
        Args:
            user_id:  사용자 ID
        Returns:
            사용자 정보 딕셔너리 (name, role 등)
        """
        return self.users.get(user_id) or self.users["yhr"]

    def leads(self) -> list[str]:
        """
        팀장 역할인 사용자 ID 목록
        Returns:
            팀장 user_id 목록
        """
        return [u for u, v in self.users.items() if v["role"] == "팀장"]

    def is_lead(self, user_id: str) -> bool:
        """
        팀장 여부 판정
        Args:
            user_id:  사용자 ID
        Returns:
            팀장이면 True
        """
        return self.user(user_id)["role"] == "팀장"

    def key_of(self, project_id: str) -> str:
        """
        프로젝트 ID를 프로젝트 키(회의·결정 ID 접두어)로 변환
        Args:
            project_id:  프로젝트 ID
        Returns:
            프로젝트 키. 찾지 못하면 "OPS"
        """
        return next((p["key"] for p in self.projects if p["project_id"] == project_id), "OPS")

    # ---------------------------------------------------------------- 권한 판정
    def _grant_valid(self, r) -> bool:
        """
        승인된 열람의 기간 확인: 1회(아직 안 씀), 7일(만료 전), 프로젝트 기간(프로젝트 진행 중), 무기한.
        Args:
            r:  access_requests 행
        Returns:
            아직 유효하면 True
        """
        # duration 컬럼이 없던 이전 버전 행: 만료일이 있으면 7일, 없으면 무기한으로 간주
        d = r["duration"] or ("7d" if r["expires"] else "forever")
        if d == "once":
            return not r["used"]
        if d in ("7d", "until"):
            return bool(r["expires"]) and r["expires"] >= now()
        if d == "project":
            # 대상 ID 접두어가 프로젝트 키 (예: OPS-M04 → OPS)
            key = r["target"].split("-")[0]
            return any(p["key"] == key and p["status"] == "진행" for p in self.projects)
        return True

    def _approved_request(self, user_id: str, target: str, consume: bool = False) -> bool:
        """
        승인된 열람 요청으로 대상을 볼 수 있는지 판정

        범위(scope)별 판정: all은 누구나, role은 요청자와 같은 역할, user는 요청자 본인.
        Args:
            user_id:  열람하려는 사용자 ID
            target:  회의 ID 또는 결정 ID
            consume:  True면 1회 열람권을 사용 처리
        Returns:
            유효한 승인이 하나라도 있으면 True
        """
        u = self.user(user_id)
        for r in self.conn.execute("SELECT * FROM access_requests WHERE target=? AND status='approved'", (target,)):
            if not self._grant_valid(r):
                continue
            # 알 수 없는 범위 값은 가장 좁은 '이 사람만'으로 취급
            scope = r["scope"] if r["scope"] in SCOPE_KO else "user"
            if scope == "all" or (scope == "role" and self.user(r["requester"])["role"] == u["role"]) \
                    or (scope == "user" and r["requester"] == user_id):
                if consume and r["duration"] == "once":
                    with self.conn:
                        self.conn.execute("UPDATE access_requests SET used=1 WHERE id=?", (r["id"],))
                return True
        return False

    def can_view_meeting(self, user_id: str, meeting: dict | None, consume: bool = False) -> bool:
        """
        회의 원문 열람 가능 여부 판정

        consume=True는 실제로 여는 순간(회의 정리 실행)에만. 목록 표시는 1회 열람권을 쓰지 않는다.
        판정 순서: 팀장·참석자 → 관리자가 연 역할 ACL → 승인된 열람 요청.
        Args:
            user_id:  사용자 ID
            meeting:  회의 정보 (meeting_id, attendees). 없으면 None
            consume:  1회 열람권 사용 여부
        Returns:
            열람 가능하면 True
        """
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
        """
        결정 열람 가능 여부 판정

        근거 회의 중 하나라도 볼 수 있으면 열람 가능. 원문이 파기된 과거 결정은 담당자 이상.
        Args:
            user_id:  사용자 ID
            decision:  결정 정보 (decision_id, evidence)
            meetings:  meeting_id → 회의 정보 (현재 원문이 남아 있는 회의)
            consume:  결정 단위 1회 열람권 사용 여부
        Returns:
            열람 가능하면 True
        """
        u = self.user(user_id)
        if u["role"] == "팀장" or self._approved_request(user_id, decision["decision_id"], consume):
            return True
        ev_meetings = {e["meeting_id"] for e in decision.get("evidence", [])}
        alive = [meetings[m] for m in ev_meetings if m in meetings]
        # 근거 회의 원문이 모두 사라졌으면 참석자 판정이 불가능해 역할 등급으로 판정
        if not alive:
            return ROLE_RANK[u["role"]] >= ROLE_RANK["담당자"]
        return any(self.can_view_meeting(user_id, m) for m in alive)

    # ---------------------------------------------------------------- 관리자: 회의 접근 권한
    def acl(self) -> dict[str, list[str]]:
        """
        회의별로 열람이 열린 역할 목록
        Returns:
            meeting_id → 역할 목록
        """
        out: dict[str, list[str]] = {}
        for r in self.conn.execute("SELECT meeting_id, role FROM meeting_acl"):
            out.setdefault(r["meeting_id"], []).append(r["role"])
        return out

    def set_acl(self, meeting_id: str, role: str, on: bool, by: str) -> None:
        """
        회의 하나에 대해 특정 역할의 열람을 켜거나 끔
        Args:
            meeting_id:  회의 ID
            role:  역할 (인턴·팀원·담당자)
            on:  True면 허용, False면 해제
            by:  변경한 관리자 ID
        """
        with self.conn:
            if on:
                self.conn.execute("INSERT OR REPLACE INTO meeting_acl VALUES (?,?,?,?)", (meeting_id, role, by, now()))
            else:
                self.conn.execute("DELETE FROM meeting_acl WHERE meeting_id=? AND role=?", (meeting_id, role))

    # ---------------------------------------------------------------- 열람 요청
    def request_access(self, requester: str, target: str, reason: str) -> int:
        """
        열람 요청을 등록하고 팀장 전원과 요청자에게 알림
        Args:
            requester:  요청자 ID
            target:  회의 ID 또는 결정 ID
            reason:  요청 사유
        Returns:
            생성된 요청 ID
        """
        with self.conn:
            cur = self.conn.execute("INSERT INTO access_requests (requester, target, reason, created) VALUES (?,?,?,?)",
                                    (requester, target, reason, now()))
        name = self.user(requester)["name"]
        for lead in self.leads():
            self.notify(lead, f"{name}님이 {target} 열람을 요청했습니다", "request", "admin")
        self.notify(requester, f"{target} 열람 요청을 보냈습니다 (팀장 승인 대기)", "info")
        return cur.lastrowid

    def requests(self, status: str | None = "pending") -> list[dict]:
        """
        열람 요청 목록을 최신순으로 조회하고 요청자 이름·역할을 덧붙임
        Args:
            status:  상태 필터 (pending / approved / rejected). None이면 전체
        Returns:
            요청 딕셔너리 목록 (requester_name, requester_role 포함)
        """
        sql = "SELECT * FROM access_requests" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
        rows = [dict(r) for r in self.conn.execute(sql, (status,) if status else ())]
        for r in rows:
            u = self.user(r["requester"])
            r["requester_name"], r["requester_role"] = u["name"], u["role"]
        return rows

    def decide_request(self, req_id: int, by: str, approve: bool, scope: str = "user", duration: str = "once",
                       until: str = "") -> dict:
        """
        열람 요청 승인 또는 반려

        범위(이 사람만 / 같은 역할 전체 / 전체)와 기간(1회 / 프로젝트 기간 / 무기한 / 직접 입력한 날짜까지).
        결과는 요청자와 승인자 모두에게 알림으로 남긴다.
        Args:
            req_id:  요청 ID
            by:  처리한 팀장 ID
            approve:  True면 승인, False면 반려
            scope:  허용 범위 (user / role / all)
            duration:  허용 기간 (once / 7d / project / forever / until)
            until:  duration=until일 때 마감 날짜 (YYYY-MM-DD로 시작하는 문자열)
        Returns:
            {"id", "status", "scope", "duration"} 처리 결과
        Raises:
            KeyError: 요청 ID가 없을 때
        """
        r = self.conn.execute("SELECT * FROM access_requests WHERE id=?", (req_id,)).fetchone()
        if r is None:
            raise KeyError(req_id)
        # 잘못된 입력은 가장 좁은 권한(이 사람만·1회)으로 낮춰 과다 허용을 막음
        scope = scope if scope in SCOPE_KO else "user"
        duration = duration if duration in DURATION_KO else "once"
        if duration == "until" and not until:
            duration = "once"
        # 만료 시각: 7일은 지금부터 7일 뒤, 기한 지정은 그날 23:59:59까지, 나머지는 만료 없음
        expires = ((datetime.now() + timedelta(days=7)).isoformat(timespec="seconds") if duration == "7d"
                   else f"{until[:10]}T23:59:59" if duration == "until" else None)
        with self.conn:
            self.conn.execute("UPDATE access_requests SET status=?, decided_by=?, scope=?, duration=?, expires=?, "
                              "decided=? WHERE id=?",
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
    def record_proposal(self, decision_id: str, requested_by: str, source: str, to: str = "") -> None:
        """
        결정·의견 승인 요청을 기록하고 처음 들어온 경우에만 팀장에게 알림

        같은 결정이 다시 제안돼도 알림이 중복되지 않게 기존 여부를 먼저 확인. 요청자 본인이 팀장이면 본인 알림은 생략.
        Args:
            decision_id:  결정 ID
            requested_by:  요청자 ID
            source:  요청 출처 (근거 회의 ID, 또는 '회의 정리'·'의견' 같은 경로)
            to:  받는 사람 ID. 비우면 팀장 전원. 팀장이 아니면 승인은 팀장만 하므로 팀장에게도 보낸다
        """
        exists = self.conn.execute("SELECT 1 FROM proposals WHERE decision_id=?", (decision_id,)).fetchone()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO proposals VALUES (?,?,?,?)",
                              (decision_id, requested_by, source, now()))
        if not exists:
            name = self.user(requested_by)["name"]
            targets = list(dict.fromkeys(([to] if to else []) + ([] if to and self.is_lead(to) else self.leads())))
            for lead in targets:
                if lead != requested_by:
                    self.notify(lead, f"{decision_id} 승인 요청 ({name}, {source})", "request", "admin")

    def proposer(self, decision_id: str) -> str | None:
        """
        결정 승인 요청을 올린 사용자 조회
        Args:
            decision_id:  결정 ID
        Returns:
            요청자 ID. 기록이 없으면 None
        """
        r = self.conn.execute("SELECT requested_by FROM proposals WHERE decision_id=?", (decision_id,)).fetchone()
        return r["requested_by"] if r else None

    def proposal_sources(self) -> dict[str, dict]:
        """
        전체 승인 요청 기록
        Returns:
            decision_id → 요청 행 딕셔너리
        """
        return {r["decision_id"]: dict(r) for r in self.conn.execute("SELECT * FROM proposals")}

    def after_decision(self, decision_id: str, by: str, approved: bool, superseded: list[str],
                       reason: str = "") -> None:
        """
        결정 승인·반려 뒤 승인자와 요청자에게 결과 알림

        요청자가 승인자 본인이면 요청자 알림은 생략.
        Args:
            decision_id:  결정 ID
            by:  처리한 팀장 ID
            approved:  승인 여부
            superseded:  이번 승인으로 대체된 기존 결정 ID 목록
            reason:  반려 사유 (반려 시 요청자 알림에 덧붙임)
        """
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
        """
        알림 한 건 저장
        Args:
            user_id:  받는 사용자 ID
            text:  알림 문구
            kind:  알림 종류 (info / request / approved / rejected / done)
            link:  눌렀을 때 이동할 화면 (예: admin)
        """
        with self.conn:
            self.conn.execute("INSERT INTO notifications (user_id, text, kind, link, created) VALUES (?,?,?,?,?)",
                              (user_id, text, kind, link, now()))

    def notifications(self, user_id: str, limit: int = 30) -> list[dict]:
        """
        사용자의 알림을 최신순으로 조회
        Args:
            user_id:  사용자 ID
            limit:  최대 개수
        Returns:
            알림 딕셔너리 목록
        """
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]

    def mark_read(self, user_id: str) -> None:
        """
        사용자의 알림을 모두 읽음 처리
        Args:
            user_id:  사용자 ID
        """
        with self.conn:
            self.conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user_id,))

    # ---------------------------------------------------------------- 답변 재사용
    def cache_get(self, key: str, fingerprint: str) -> dict | None:
        """
        저장된 답변 조회

        key는 질문 단위, fingerprint는 답을 만들 때 쓴 입력(원장·정책·문서·원문)의 상태. 둘 다 같을 때만 재사용.
        Args:
            key:  캐시 키 (예: summary:OPS-M04)
            fingerprint:  현재 입력 상태 지문
        Returns:
            {"final", "created", ...payload} 딕셔너리. 없거나 지문이 다르면 None
        """
        r = self.conn.execute("SELECT * FROM answer_cache WHERE key=?", (key,)).fetchone()
        if r is None or r["fingerprint"] != fingerprint:
            return None            # 처음이거나, 원장·정책·문서·원문 중 무엇이 바뀌었으면 새로 만든다
        return {"final": r["final"], "created": r["created"], **json.loads(r["payload"])}

    def cache_clear(self) -> None:
        """
        저장 답변 전체 삭제

        모델을 바꾸면 이전 모델로 만든 답이 섞이지 않게 저장 답변을 비운다.
        """
        with self.conn:
            self.conn.execute("DELETE FROM answer_cache")

    def cache_put(self, key: str, fingerprint: str, final: str, payload: dict) -> None:
        """
        답변 저장 (같은 key면 덮어씀)

        payload의 datetime 등 JSON에 없는 타입은 default=str로 문자열화.
        Args:
            key:  캐시 키
            fingerprint:  답을 만들 때의 입력 상태 지문
            final:  최종 답변 문자열
            payload:  답변과 함께 돌려줄 부가 데이터
        """
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO answer_cache VALUES (?,?,?,?,?)",
                              (key, fingerprint, final, json.dumps(payload, ensure_ascii=False, default=str), now()))

    # ---------------------------------------------------------------- 대화방
    def touch_thread(self, thread_id: str, user_id: str, title: str, project_key: str | None,
                     meeting_id: str | None) -> None:
        """
        대화방 생성 또는 갱신

        처음이면 첫 메시지 앞 40자를 제목으로 만들고, 이미 있으면 갱신 시각만 바꾼다.
        프로젝트·회의는 COALESCE로 처음 정해진 값을 유지해 대화방 분류가 중간에 바뀌지 않게 함.
        Args:
            thread_id:  대화방 ID
            user_id:  소유자 ID
            title:  제목 후보 (보통 첫 메시지)
            project_key:  관련 프로젝트 키. 없으면 None
            meeting_id:  관련 회의 ID. 없으면 None
        """
        r = self.conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        with self.conn:
            if r is None:
                self.conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)",
                                  (thread_id, user_id, title[:40], project_key, meeting_id, now()))
            else:
                self.conn.execute("UPDATE threads SET updated=?, project_key=COALESCE(project_key, ?), "
                                  "meeting_id=COALESCE(meeting_id, ?) WHERE thread_id=?",
                                  (now(), project_key, meeting_id, thread_id))

    def thread_refs(self, thread_id: str) -> list[str]:
        """
        대화방에서 직전 답이 가리킨 대상 (결정·회의 ID) — '이거', '그 회의'를 코드가 풀 때 쓴다
        Args:
            thread_id: 대화방 ID
        Returns:
            ID 목록 (앞이 더 중요), 없으면 빈 목록
        """
        r = self.conn.execute("SELECT refs FROM thread_refs WHERE thread_id=?", (thread_id,)).fetchone()
        return json.loads(r["refs"]) if r and r["refs"] else []

    def set_thread_refs(self, thread_id: str, refs: list[str]) -> None:
        """
        직전 답이 가리킨 대상 저장 (비어 있으면 이전 값을 유지)
        Args:
            thread_id: 대화방 ID
            refs:      결정·회의 ID 목록
        """
        if not refs:
            return
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO thread_refs VALUES (?,?)",
                              (thread_id, json.dumps(refs[:10], ensure_ascii=False)))

    def delete_thread(self, thread_id: str, user_id: str) -> bool:
        """
        대화방 삭제

        본인 대화방만 지운다 (대화 내용은 서버가 session.db에서 함께 지움).
        Args:
            thread_id:  대화방 ID
            user_id:  요청한 사용자 ID
        Returns:
            실제로 지웠으면 True, 없거나 남의 대화방이면 False
        """
        with self.conn:
            cur = self.conn.execute("DELETE FROM threads WHERE thread_id=? AND user_id=?", (thread_id, user_id))
        return cur.rowcount > 0

    def threads(self, user_id: str) -> list[dict]:
        """
        사용자의 대화방을 최근 갱신순으로 조회
        Args:
            user_id:  사용자 ID
        Returns:
            대화방 딕셔너리 목록
        """
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM threads WHERE user_id=? ORDER BY updated DESC", (user_id,))]

    def reset(self) -> None:
        """
        모든 테이블의 행 삭제 (스키마는 유지)

        데모 시드(scripts/seed_demo.py) 초기화용.
        """
        with self.conn:
            for t in ("meeting_acl", "access_requests", "proposals", "notifications", "threads", "answer_cache"):
                self.conn.execute(f"DELETE FROM {t}")
