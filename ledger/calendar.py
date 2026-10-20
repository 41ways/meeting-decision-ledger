"""일정 저장소 — CalendarProvider 인터페이스와 구현 두 개.

- LocalCalendarProvider : SQLite `store/calendar.db`의 events 표. 실제로 동작하는 기본 구현
- GoogleCalendarProvider: 구글 팀 달력 연동 자리. 필드 매핑·설정 읽기·호출 지점만 있고 네트워크 호출은 하지 않는다
                          (dry-run이면 보낼 요청을 로그로 남기고, 아니면 NotImplementedError)

고르는 기준은 환경변수 CALENDAR_PROVIDER=local|google (build_calendar). 그래프·서버는 인터페이스만 본다.
연결 절차는 docs/calendar_integration.md.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = Path(os.environ.get("LEDGER_STORE", ROOT / "store"))   # 평가 사본(LEDGER_STORE)과 섞이지 않게 같은 기준
FIELDS = ("event_id", "title", "date", "start", "end", "attendees", "project", "kind", "created_by", "created",
          "source", "external_id")
log = logging.getLogger("calendar")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, title TEXT, date TEXT, start TEXT, end TEXT,
    attendees TEXT, project TEXT, kind TEXT, created_by TEXT, created TEXT, source TEXT DEFAULT 'local',
    external_id TEXT);
"""


class CalendarProvider(Protocol):
    """
    일정 저장소 인터페이스 — 그래프·서버·MCP는 이것만 본다

    일정 dict 키: event_id, title, date(YYYY-MM-DD), start·end(HH:MM, end는 빈 문자열 가능),
    attendees(이름 목록), project(프로젝트 키), kind(meeting|interview|event), created_by, created,
    source(local|google), external_id(외부 달력 이벤트 ID).
    """
    name: str

    def list_events(self, date_from: str = "", date_to: str = "") -> list[dict]:
        """
        기간 안의 일정 목록
        Args:
            date_from: 시작일 (YYYY-MM-DD, 비우면 처음부터)
            date_to: 종료일 (YYYY-MM-DD, 비우면 끝까지, 이 날 포함)
        Returns:
            일정 목록 (날짜·시작 시간 순)
        """

    def create_event(self, event: dict) -> dict:
        """
        일정 저장 — 사람 확인을 거친 초안만 들어온다
        Args:
            event: 검증된 일정 초안
        Returns:
            저장된 일정 (event_id 포함)
        """

    def update_event(self, event_id: str, changes: dict) -> dict:
        """
        일정 수정
        Args:
            event_id: 일정 ID
            changes: 바꿀 필드
        Returns:
            수정된 일정
        """

    def delete_event(self, event_id: str) -> bool:
        """
        일정 삭제
        Args:
            event_id: 일정 ID
        Returns:
            지웠으면 True
        """


class LocalCalendarProvider:
    """
    SQLite events 표에 일정을 두는 기본 구현

    Attributes:
        name: 'local'
        conn: calendar.db 연결
    """
    name = "local"

    def __init__(self, db: Path | str | None = None):
        """
        DB를 열고 events 표를 만든다
        Args:
            db: DB 경로. 없으면 CALENDAR_DB 환경변수, 그것도 없으면 store/calendar.db. ':memory:'도 된다
        """
        db = db or os.environ.get("CALENDAR_DB") or STORE_DIR / "calendar.db"
        if str(db) != ":memory:":
            Path(db).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        """
        DB 행을 일정 dict로 변환 (참석자는 JSON 문자열 → 목록)
        Args:
            r: events 행
        Returns:
            일정 dict
        """
        d = dict(r)
        d["attendees"] = json.loads(d["attendees"] or "[]")
        return d

    def list_events(self, date_from: str = "", date_to: str = "") -> list[dict]:
        """
        기간 안의 일정 목록 (ISO 날짜 문자열 비교 = 날짜 비교)
        Args:
            date_from: 시작일 (비우면 처음부터)
            date_to: 종료일 (비우면 끝까지, 이 날 포함)
        Returns:
            일정 목록 (날짜·시작 시간 순)
        """
        rows = self.conn.execute("SELECT * FROM events WHERE date >= ? AND date <= ? ORDER BY date, start",
                                 (date_from or "0000-00-00", date_to or "9999-99-99")).fetchall()
        return [self._row(r) for r in rows]

    def get_event(self, event_id: str) -> dict | None:
        """
        일정 하나 조회
        Args:
            event_id: 일정 ID
        Returns:
            일정 dict. 없으면 None
        """
        r = self.conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return self._row(r) if r else None

    def create_event(self, event: dict) -> dict:
        """
        일정 저장. ID는 EVT-0001부터 차례로 붙인다
        Args:
            event: 검증된 일정 초안
        Returns:
            저장된 일정 (event_id·created 포함)
        """
        n = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        row = {k: event.get(k) or "" for k in FIELDS} | {
            "event_id": f"EVT-{n + 1:04d}", "created": datetime.now().isoformat(timespec="seconds"),
            "source": event.get("source") or "local", "attendees": json.dumps(event.get("attendees") or [],
                                                                               ensure_ascii=False)}
        self.conn.execute(f"INSERT INTO events ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
                          [row[k] for k in FIELDS])
        self.conn.commit()
        return self.get_event(row["event_id"])

    def update_event(self, event_id: str, changes: dict) -> dict:
        """
        일정 수정 (ID·생성 시각은 바꾸지 않음)
        Args:
            event_id: 일정 ID
            changes: 바꿀 필드
        Returns:
            수정된 일정
        Raises:
            KeyError: 없는 일정
        """
        cols = {k: v for k, v in changes.items() if k in FIELDS and k not in ("event_id", "created")}
        if "attendees" in cols:
            cols["attendees"] = json.dumps(cols["attendees"], ensure_ascii=False)
        if cols:
            self.conn.execute(f"UPDATE events SET {', '.join(f'{k} = ?' for k in cols)} WHERE event_id = ?",
                              [*cols.values(), event_id])
            self.conn.commit()
        ev = self.get_event(event_id)
        if ev is None:
            raise KeyError(event_id)
        return ev

    def delete_event(self, event_id: str) -> bool:
        """
        일정 삭제
        Args:
            event_id: 일정 ID
        Returns:
            지웠으면 True
        """
        cur = self.conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
        self.conn.commit()
        return cur.rowcount > 0


class GoogleCalendarProvider:
    """
    구글 팀 달력 연동 자리 (스텁) — 필드 매핑과 호출 지점만 있고 네트워크 호출은 하지 않는다

    Google Calendar API v3 기준. 실제 연결 전 체크리스트는 docs/calendar_integration.md.
    Attributes:
        name: 'google'
        calendar_id: 팀 달력 ID (GOOGLE_CALENDAR_ID)
        credentials_file: 서비스 계정 키 JSON 경로 (GOOGLE_APPLICATION_CREDENTIALS). 파일을 읽지는 않는다
        dry_run: True면 보낼 요청을 sent에 쌓고 로그만 남긴다
        sent: dry-run에서 만든 요청 목록 (method, path, body)
    """
    name = "google"
    BASE = "https://www.googleapis.com/calendar/v3"
    SCOPE = "https://www.googleapis.com/auth/calendar.events"
    TZ = "Asia/Seoul"

    def __init__(self, calendar_id: str = "", credentials_file: str = "", dry_run: bool = True):
        """
        설정만 보관한다 (인증·연결 없음)
        Args:
            calendar_id: 팀 달력 ID
            credentials_file: 자격 증명 파일 경로
            dry_run: 요청을 만들기만 하고 보내지 않을지
        """
        self.calendar_id = calendar_id
        self.credentials_file = credentials_file
        self.dry_run = dry_run
        self.sent: list[dict] = []

    @classmethod
    def from_env(cls, env: dict | None = None) -> "GoogleCalendarProvider":
        """
        환경변수에서 설정 읽기
        Args:
            env: 환경변수 사전 (테스트용). 없으면 os.environ
        Returns:
            GoogleCalendarProvider
        """
        env = os.environ if env is None else env
        return cls(env.get("GOOGLE_CALENDAR_ID", ""), env.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
                   env.get("GOOGLE_CALENDAR_DRY_RUN", "true").lower() != "false")

    @classmethod
    def to_google(cls, ev: dict) -> dict:
        """
        우리 일정 → Google Calendar Event 리소스

        users.json에 이메일이 없어 참석자는 attendees가 아니라 설명과 extendedProperties에 이름으로 넣는다.
        끝 시간이 없으면 시작 +1시간 (구글은 end가 필수). 시작 시간이 없으면 종일 일정.
        Args:
            ev: 일정 dict
        Returns:
            events.insert 요청 본문
        """
        if ev.get("start"):
            end = ev.get("end") or (datetime.strptime(ev["start"], "%H:%M") + timedelta(hours=1)).strftime("%H:%M")
            start_v = {"dateTime": f"{ev['date']}T{ev['start']}:00", "timeZone": cls.TZ}
            end_v = {"dateTime": f"{ev['date']}T{end}:00", "timeZone": cls.TZ}
        else:
            start_v = {"date": ev["date"]}
            end_v = {"date": (date.fromisoformat(ev["date"]) + timedelta(days=1)).isoformat()}
        names = ", ".join(ev.get("attendees") or [])
        return {"summary": ev["title"], "start": start_v, "end": end_v,
                "description": f"참석: {names}\n프로젝트: {ev.get('project') or '-'}\n(회의 요약·결정 정리 비서에서 추가)",
                "extendedProperties": {"private": {"ledger_event_id": ev.get("event_id", ""),
                                                   "project": ev.get("project") or "", "kind": ev.get("kind") or "",
                                                   "attendees": json.dumps(ev.get("attendees") or [],
                                                                           ensure_ascii=False)}}}

    @staticmethod
    def from_google(item: dict) -> dict:
        """
        Google Calendar Event 리소스 → 우리 일정
        Args:
            item: events.list 응답의 items 한 건
        Returns:
            일정 dict (source='google', external_id=구글 이벤트 ID)
        """
        priv = (item.get("extendedProperties") or {}).get("private") or {}
        s, e = item.get("start") or {}, item.get("end") or {}
        dt_s, dt_e = s.get("dateTime", ""), e.get("dateTime", "")
        return {"event_id": priv.get("ledger_event_id", ""), "title": item.get("summary", ""),
                "date": (dt_s or s.get("date", ""))[:10], "start": dt_s[11:16], "end": dt_e[11:16],
                "attendees": json.loads(priv.get("attendees") or "[]"), "project": priv.get("project", ""),
                "kind": priv.get("kind", "event"), "created_by": (item.get("creator") or {}).get("email", ""),
                "created": item.get("created", ""), "source": "google", "external_id": item.get("id", "")}

    def _request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        """
        구글 API 호출 지점 — 지금은 보내지 않는다
        Args:
            method: HTTP 메서드
            path: BASE 뒤 경로
            body: 요청 본문
            params: 쿼리 파라미터
        Returns:
            dry-run이면 None
        Raises:
            NotImplementedError: dry-run이 아닐 때 (인증·HTTP 클라이언트 미구현)
        """
        req = {"method": method, "path": path, "params": params or {}, "body": body}
        if self.dry_run:
            self.sent.append(req)
            log.info("google calendar dry-run: %s", json.dumps(req, ensure_ascii=False))
            return None
        raise NotImplementedError("구글 달력 실제 호출은 아직 없습니다. docs/calendar_integration.md 체크리스트 참고")

    def list_events(self, date_from: str = "", date_to: str = "") -> list[dict]:
        """
        팀 달력 일정 목록 (events.list). dry-run이면 빈 목록
        Args:
            date_from: 시작일
            date_to: 종료일 (이 날 포함)
        Returns:
            일정 목록
        """
        params = {"singleEvents": "true", "orderBy": "startTime"}
        if date_from:
            params["timeMin"] = f"{date_from}T00:00:00+09:00"
        if date_to:
            params["timeMax"] = f"{date.fromisoformat(date_to) + timedelta(days=1)}T00:00:00+09:00"
        res = self._request("GET", f"/calendars/{self.calendar_id}/events", params=params)
        return [self.from_google(x) for x in (res or {}).get("items", [])]

    def create_event(self, event: dict) -> dict:
        """
        팀 달력에 일정 추가 (events.insert). dry-run이면 저장하지 않고 dry_run=True로 돌려준다
        Args:
            event: 검증된 일정 초안
        Returns:
            저장된 일정, dry-run이면 보낼 뻔한 일정
        """
        res = self._request("POST", f"/calendars/{self.calendar_id}/events", body=self.to_google(event))
        if res is None:
            return {**event, "event_id": "", "source": "google", "external_id": "", "dry_run": True}
        return self.from_google(res)

    def update_event(self, event_id: str, changes: dict) -> dict:
        """
        팀 달력 일정 수정 (events.patch). event_id는 구글 이벤트 ID
        Args:
            event_id: 구글 이벤트 ID
            changes: 바꿀 필드 (우리 일정 형식)
        Returns:
            수정된 일정, dry-run이면 changes를 그대로
        """
        body = {}
        if "title" in changes:
            body["summary"] = changes["title"]
        if "date" in changes:       # 날짜·시간은 start·end 묶음으로만 바꿀 수 있다
            full = self.to_google({"title": "", **changes})
            body["start"], body["end"] = full["start"], full["end"]
        res = self._request("PATCH", f"/calendars/{self.calendar_id}/events/{event_id}", body=body)
        return {**changes, "external_id": event_id, "dry_run": True} if res is None else self.from_google(res)

    def delete_event(self, event_id: str) -> bool:
        """
        팀 달력 일정 삭제 (events.delete)
        Args:
            event_id: 구글 이벤트 ID
        Returns:
            dry-run이면 False (지우지 않음)
        """
        return self._request("DELETE", f"/calendars/{self.calendar_id}/events/{event_id}") is not None


def build_calendar(env: dict | None = None) -> CalendarProvider:
    """
    환경변수 CALENDAR_PROVIDER로 일정 저장소 고르기
    Args:
        env: 환경변수 사전 (테스트용). 없으면 os.environ
    Returns:
        local(기본)이면 LocalCalendarProvider, google이면 GoogleCalendarProvider
    Raises:
        ValueError: 모르는 값
    """
    env = os.environ if env is None else env
    kind = env.get("CALENDAR_PROVIDER", "local").lower()
    if kind == "local":
        return LocalCalendarProvider(env.get("CALENDAR_DB"))
    if kind == "google":
        return GoogleCalendarProvider.from_env(env)
    raise ValueError(f"CALENDAR_PROVIDER는 local 또는 google: {kind}")


# ---------------------------------------------------------------- 겹침 검사
def _minutes(hm: str) -> int:
    """
    'HH:MM'을 자정부터 분 수로
    Args:
        hm: 'HH:MM'
    Returns:
        분 수
    """
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def _span(item: dict) -> tuple[int, int] | None:
    """
    일정·회의의 시간 구간 (분). 끝이 없으면 시작부터 1시간
    Args:
        item: start·end가 있는 일정 또는 회의
    Returns:
        (시작 분, 끝 분). 시작 시간을 모르면 None
    """
    if not item.get("start"):
        return None
    s = _minutes(item["start"])
    e = _minutes(item["end"]) if item.get("end") else s + 60
    return s, max(e, s + 1)


def find_conflicts(draft: dict, events: list[dict], meetings: list[dict]) -> list[dict]:
    """
    같은 참석자의 같은 시간대 일정·회의 찾기

    회의 시간은 회의록 첫·마지막 발언 시각(list_meetings의 start·end). 시각이 없는 회의(면접 녹취 등)는
    같은 날이면 '시간 미상'으로 함께 알린다.
    Args:
        draft: 일정 초안 (date, start, end, attendees)
        events: 저장된 일정 목록
        meetings: 회의 목록 (meeting_id, date, title, attendees, start, end)
    Returns:
        겹치는 항목 목록 — id, title, date, start, end, who(겹치는 참석자), exact(시간까지 겹치는지)
    """
    mine = _span(draft)
    people = draft.get("attendees") or []
    out = []
    items = [{**e, "id": e.get("event_id") or e.get("external_id")} for e in events] + \
            [{**m, "id": m["meeting_id"]} for m in meetings]
    for it in items:
        if it.get("date") != draft.get("date") or (draft.get("event_id") and it["id"] == draft["event_id"]):
            continue
        who = [n for n in people if n in (it.get("attendees") or [])]     # 초안의 참석자 순서대로
        if not who:
            continue
        theirs = _span(it)
        if mine and theirs and not (mine[0] < theirs[1] and theirs[0] < mine[1]):
            continue
        out.append({"id": it["id"], "title": it.get("title", ""), "date": it["date"], "start": it.get("start") or "",
                    "end": it.get("end") or "", "who": who, "exact": bool(mine and theirs)})
    return out
