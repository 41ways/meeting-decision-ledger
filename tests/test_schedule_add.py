"""일정 추가 테스트 — 날짜·시간 해석, 겹침, 권한, route 분기, 확인 전 저장 안 됨, 저장소 가짜 주입.

LLM·MCP는 가짜, 저장소는 메모리 SQLite(':memory:')나 가짜 provider를 끼운다. 기준 날짜는 2026-10-20(화).
"""
# pylint: disable=missing-function-docstring,missing-class-docstring,redefined-outer-name,unused-argument  # pytest fixture·가짜 객체 관용
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import ledger_graph as G
from ledger.access import AccessStore
from ledger.calendar import GoogleCalendarProvider, LocalCalendarProvider, build_calendar, find_conflicts
from ledger.calendar_api import router
from ledger.schedule_parse import add_permission, build_draft, is_add_request, parse_when, week_offset

TODAY = "2026-10-20"
M05 = {"meeting_id": "OPS-M05", "date": "2026-10-20", "title": "현업 피드백 회의", "domain": "meeting",
       "attendees": ["정한결", "서지후", "장상민", "윤하린"], "start": "11:00", "end": "11:27"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 날짜·시간 해석 (코드)
@pytest.mark.parametrize("msg,day,start,end", [
    ("다음 주 화요일 오후 3시에 OPS 주간 회의 잡아줘", "2026-10-27", "15:00", ""),
    ("내일 10시 반 장상민이랑 캐시 설계 리뷰 30분", "2026-10-21", "10:30", "11:00"),
    ("10월 28일 14:00~15:00 보안 점검 일정 추가", "2026-10-28", "14:00", "15:00"),
    ("다다음 주 금요일 오후 2시~4시 회고 잡아줘", "2026-11-06", "14:00", "16:00"),
    ("모레 오전 9시 배포 점검 1시간 반 잡아줘", "2026-10-22", "09:00", "10:30"),
    ("금요일 3시부터 5시까지 워크숍 등록", "2026-10-23", "15:00", "17:00"),
    ("2026-11-02 13:30 보안 교육 추가", "2026-11-02", "13:30", ""),
])
def test_parse_when(msg, day, start, end):
    w = parse_when(msg, TODAY)
    assert (w["date"], w["start"], w["end"]) == (day, start, end), w


def test_time_missing_is_left_blank_and_asked():
    d = build_draft("다음 주 금요일 회의 잡아줘", TODAY, "kjh")
    assert d["date"] == "2026-10-30" and d["start"] == "" and d["missing"] == ["start"]
    assert d["saved"] is False


def test_pm_assumption_is_disclosed():
    d = build_draft("금요일 3시 미팅 추가", TODAY, "kjh")
    assert d["start"] == "15:00" and any("오후 3시로 봤습니다" in n for n in d["notes"])


def test_week_offset_matches_graph_range():
    """일정 추가와 일정 조회가 '다음 주'를 같은 주로 계산해야 한다 (규칙을 두 곳에 두지 않도록 대조)."""
    for q in ("지지난주", "저저번 주", "전전주", "지난주", "저번주", "전주", "다음 주", "다다음 주", "3주 뒤", "2주 전"):
        a, _, _ = G._range(q + " 회의", TODAY)        # pylint: disable=protected-access
        mon = parse_when(q + " 월요일 10시 회의 추가", TODAY)["date"]
        assert a == mon, (q, a, mon, week_offset(q))


def test_draft_fields_and_validation():
    d = build_draft("내일 10시 반 장상민이랑 캐시 설계 리뷰 30분", TODAY, "kjh")
    assert d["title"] == "캐시 설계 리뷰" and d["attendees"] == ["정한결", "장상민"] and d["kind"] == "meeting"
    d = build_draft("다음 주 화요일 오후 3시에 OPS 주간 회의 잡아줘", TODAY, "pdy")
    assert d["project"] == "OPS" and d["title"] == "OPS 주간 회의" and d["attendees"] == ["서지후"]
    d = build_draft("10월 28일 14:00~15:00 CSB 보안 점검 일정 추가", TODAY, "kjh")
    assert d["project"] == "CSB" and any("종료된 프로젝트" in w for w in d["warnings"])


def test_llm_values_checked_against_source():
    """LLM이 원문에 없는 제목·사람을 내면 버리고, 원문에 있지만 등록 안 된 이름은 경고로."""
    d = build_draft("금요일 3시 김철수랑 미팅 추가", TODAY, "kjh",
                    {"title": "주간 전략 회의", "attendees": ["김철수", "김태림"], "project": "OPS"})
    assert d["title_source"] == "code" and d["title"] == "김철수랑 미팅"
    assert d["attendees"] == ["정한결"] and d["project"] == ""
    assert any("김철수" in w for w in d["warnings"]) and not any("김태림" in w for w in d["warnings"])
    d = build_draft("내일 10시 상민 님하고 캐시 리뷰 추가", TODAY, "kjh",
                    {"title": "캐시 리뷰", "attendees": ["상민"], "project": ""})
    assert d["title_source"] == "llm" and d["attendees"] == ["정한결", "장상민"]


# ---------------------------------------------------------------- route 분기 (조회 vs 추가)
@pytest.mark.parametrize("msg,add", [
    ("다음 주 화요일 오후 3시에 OPS 주간 회의 잡아줘", True),
    ("내일 10시 반 장상민이랑 캐시 설계 리뷰 30분", True),
    ("10월 28일 14:00~15:00 보안 점검 일정 추가", True),
    ("다음 주 금요일 회의 잡아줘", True),
    ("이번 주 회의 몇 개야?", False),
    ("다음 주 회의 추가됐어?", False),
    ("지난주 회의 정리해줘", False),
    ("다음 주에 캐시 추가하자", False),
    ("검증 노드 기준에 근거 인용 여부를 추가하면 어떨까요?", False),
])
def test_is_add_request(msg, add):
    assert is_add_request(msg) is add


def test_eval_questions_do_not_route_to_schedule_add():
    gt = json.loads(open("eval/ground_truth.json", encoding="utf-8").read())
    for x in gt["opinions"] + gt["questions"]:
        assert not is_add_request(x["text"]), x["text"]


# ---------------------------------------------------------------- 겹침
def test_conflict_with_meeting_and_event():
    d = build_draft("오늘 11시 장상민이랑 캐시 리뷰 30분 잡아줘", TODAY, "pdy")
    cs = find_conflicts(d, [], [M05])
    assert [c["id"] for c in cs] == ["OPS-M05"] and cs[0]["who"] == ["서지후", "장상민"] and cs[0]["exact"]
    later = build_draft("오늘 13시 장상민이랑 캐시 리뷰 30분 잡아줘", TODAY, "pdy")
    assert find_conflicts(later, [], [M05]) == []
    other = build_draft("오늘 11시 노영오랑 리뷰 추가", TODAY, "cyn")          # 노영오는 M05 목록에 없음(가짜 데이터)
    assert find_conflicts(other, [], [M05]) == []
    ev = {"event_id": "EVT-0001", "date": TODAY, "start": "13:15", "end": "", "title": "1:1", "attendees": ["장상민"]}
    assert [c["id"] for c in find_conflicts(later, [ev], [])] == ["EVT-0001"]    # 끝 없는 일정은 1시간으로 본다


def test_interview_without_clock_is_same_day_warning():
    iv = {"meeting_id": "HBE-I01", "date": "2026-10-16", "title": "면접", "attendees": ["서지후"], "start": "", "end": ""}
    d = build_draft("10월 16일 오후 5시 서지후 님하고 회고 잡아줘", TODAY, "kjh")
    cs = find_conflicts(d, [], [iv])
    assert cs and cs[0]["exact"] is False


# ---------------------------------------------------------------- 권한
def test_add_permission():
    assert add_permission("kjh", ["정한결", "장상민"])[0] == "add"
    assert add_permission("lsj", ["장상민", "서지후"])[0] == "add"                  # 팀원 이상은 바로 추가
    assert add_permission("yhr", ["윤하린"]) == ("add", "본인 일정")                # 인턴은 본인 일정만
    assert add_permission("yhr", ["윤하린", "장상민"])[0] == "request"
    assert add_permission("nobody", [])[0] == "deny"


# ---------------------------------------------------------------- 그래프 (가짜 LLM·MCP·저장소)
class FakeLLM:
    name = "fake"

    async def json(self, system, user, schema, **kw):
        if "title" in schema["properties"]:
            return {"title": "OPS 주간 회의", "attendees": [], "project": "OPS"}, {"tokens": 3}
        return {"mode": "chat"}, {"tokens": 1}

    async def text(self, system, user, on_token=None, **kw):
        return "안녕하세요", {"tokens": 1}


class FakeMCP:
    calls = 0

    async def call(self, name, **a):
        assert name == "list_meetings", name
        return [M05]


class FakeCalendar:
    """CalendarProvider 가짜 — 그래프가 인터페이스만 보는지 확인"""
    name = "fake"

    def __init__(self, events=()):
        self.events, self.created = list(events), []

    def list_events(self, date_from="", date_to=""):
        return [e for e in self.events if (date_from or "0") <= e["date"] <= (date_to or "9")]

    def create_event(self, event):
        self.created.append(event)
        return event


def _graph(cal):
    return G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=FakeMCP(),
                                              calendar=cal, today=TODAY))


def test_graph_drafts_but_does_not_save():
    cal = LocalCalendarProvider(":memory:")
    st = run(_graph(cal).ainvoke({"message": "오늘 오전 11시 장상민이랑 OPS 주간 회의 잡아줘", "user_id": "kjh",
                                  "events": []}))
    assert st["mode"] == "schedule_add" and st["schedule_draft"]["saved"] is False
    assert st["schedule_draft"]["title"] == "OPS 주간 회의" and st["schedule_draft"]["title_source"] == "llm"
    assert "겹침: 정한결, 장상민 — OPS-M05" in st["final"] and "'추가'를 눌러야 저장" in st["final"]
    assert cal.list_events() == []                                  # 사람 확인 전에는 저장하지 않는다
    assert [e["type"] for e in st["events"]] == ["RouteDecided", "ScheduleDrafted", "Finalize"]


def test_graph_asks_when_time_missing():
    st = run(_graph(FakeCalendar()).ainvoke({"message": "다음 주 금요일 회의 잡아줘", "user_id": "kjh", "events": []}))
    assert st["schedule_draft"]["missing"] == ["start"] and "되물음: 비어 있는 항목 — 시작 시간" in st["final"]


def test_graph_works_when_llm_fails():
    class BrokenLLM(FakeLLM):
        async def json(self, system, user, schema, **kw):
            raise RuntimeError("ollama down")
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=BrokenLLM(), quality_llm=BrokenLLM(), mcp=FakeMCP(),
                                              calendar=FakeCalendar(), today=TODAY))
    st = run(graph.ainvoke({"message": "10월 28일 14:00~15:00 보안 점검 일정 추가", "user_id": "kjh", "events": []}))
    assert st["schedule_draft"]["title"] == "보안 점검" and st["schedule_draft"]["title_source"] == "code"


def test_schedule_answer_includes_added_events():
    cal = FakeCalendar([{"event_id": "EVT-0001", "date": "2026-10-22", "start": "14:00", "end": "15:00",
                         "title": "보안 점검", "attendees": ["김태림"], "kind": "event"},
                        {"event_id": "EVT-0002", "date": "2026-10-23", "start": "10:00", "end": "",
                         "title": "OPS 주간 회의", "attendees": ["정한결"], "kind": "meeting"}])
    graph = _graph(cal)
    st = run(graph.ainvoke({"message": "이번 주 일정 몇 개야?", "events": []}))
    assert st["mode"] == "schedule"
    assert st["final"].startswith("이번 주(10-19~10-25) 일정은 3건입니다 (회의 2건, 면접 0건, 기타 일정 1건)"), st["final"]
    assert "EVT-0001 · 10-22(목) 14:00~15:00 보안 점검" in st["final"]
    st = run(graph.ainvoke({"message": "이번 주 회의 몇 개야?", "events": []}))
    assert st["final"].startswith("이번 주(10-19~10-25) 회의는 2건") and "EVT-0001" not in st["final"]


# ---------------------------------------------------------------- 저장소
def test_local_provider_crud():
    cal = LocalCalendarProvider(":memory:")
    ev = cal.create_event({"date": "2026-10-27", "start": "15:00", "end": "", "title": "OPS 주간 회의",
                           "attendees": ["정한결"], "project": "OPS", "kind": "meeting", "created_by": "kjh"})
    assert ev["event_id"] == "EVT-0001" and ev["attendees"] == ["정한결"] and ev["source"] == "local"
    assert [e["event_id"] for e in cal.list_events("2026-10-26", "2026-11-01")] == ["EVT-0001"]
    assert cal.list_events("2026-10-28", "") == []
    assert cal.update_event("EVT-0001", {"end": "16:00"})["end"] == "16:00"
    assert cal.delete_event("EVT-0001") and cal.list_events() == []


def test_google_stub_maps_fields_without_network():
    g = GoogleCalendarProvider("team@group.calendar.google.com", "/nowhere/key.json", dry_run=True)
    ev = {"event_id": "EVT-0007", "date": "2026-10-27", "start": "15:00", "end": "", "title": "OPS 주간 회의",
          "attendees": ["정한결", "서지후"], "project": "OPS", "kind": "meeting"}
    body = GoogleCalendarProvider.to_google(ev)
    assert body["start"] == {"dateTime": "2026-10-27T15:00:00", "timeZone": "Asia/Seoul"}
    assert body["end"]["dateTime"] == "2026-10-27T16:00:00"                       # 구글은 end 필수 → 시작 +1시간
    assert body["extendedProperties"]["private"]["ledger_event_id"] == "EVT-0007"
    back = GoogleCalendarProvider.from_google({**body, "id": "g123"})
    assert (back["date"], back["start"], back["attendees"], back["external_id"]) == \
        ("2026-10-27", "15:00", ["정한결", "서지후"], "g123")
    out = g.create_event(ev)
    assert out["dry_run"] and g.sent[0]["method"] == "POST" and g.list_events("2026-10-27", "2026-10-27") == []
    with pytest.raises(NotImplementedError):
        GoogleCalendarProvider("x", dry_run=False).create_event(ev)


def test_build_calendar_from_env(tmp_path):
    assert isinstance(build_calendar({"CALENDAR_PROVIDER": "local", "CALENDAR_DB": str(tmp_path / "c.db")}),
                      LocalCalendarProvider)
    g = build_calendar({"CALENDAR_PROVIDER": "google", "GOOGLE_CALENDAR_ID": "team-cal"})
    assert isinstance(g, GoogleCalendarProvider) and g.calendar_id == "team-cal" and g.dry_run
    with pytest.raises(ValueError):
        build_calendar({"CALENDAR_PROVIDER": "outlook"})


# ---------------------------------------------------------------- 확인 API (저장은 여기서만)
class _Deps:
    def __init__(self, cal):
        self.calendar, self.mcp = cal, FakeMCP()


@pytest.fixture()
def client(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.deps = _Deps(LocalCalendarProvider(":memory:"))
    app.state.access = AccessStore(tmp_path / "access.db")
    return TestClient(app)


def test_api_preview_does_not_save_and_post_saves(client):
    draft = build_draft("오늘 오전 11시 장상민이랑 캐시 리뷰 30분 잡아줘", TODAY, "lsj")
    r = client.post("/events/preview", json={"user": "lsj", "draft": draft}).json()
    assert r["errors"] == [] and r["conflicts"][0]["id"] == "OPS-M05" and r["permission"] == "add"
    assert client.get("/events").json()["events"] == []
    r = client.post("/events", json={"user": "lsj", "draft": {**draft, "attendees": "장상민, 노영오"}}).json()
    assert r["event"]["event_id"] == "EVT-0001" and r["event"]["attendees"] == ["장상민", "노영오"]
    assert [e["title"] for e in client.get("/events").json()["events"]] == ["캐시 리뷰"]
    acc = client.app.state.access
    assert any("일정을 추가했습니다" in n["text"] for n in acc.notifications("cyn"))


def test_api_rejects_blank_time_and_unknown_people(client):
    draft = build_draft("다음 주 금요일 회의 잡아줘", TODAY, "kjh")
    r = client.post("/events", json={"user": "kjh", "draft": draft})
    assert r.status_code == 400 and "시작 시간" in r.json()["detail"]
    r = client.post("/events", json={"user": "kjh", "draft": {**draft, "start": "10:00", "attendees": ["김철수"]}})
    assert r.status_code == 400 and "김철수" in r.json()["detail"]
    assert client.get("/events").json()["events"] == []


def test_api_intern_request_goes_to_lead(client):
    draft = build_draft("내일 10시 장상민이랑 온보딩 질문 30분 잡아줘", TODAY, "yhr")
    r = client.post("/events", json={"user": "yhr", "draft": draft}).json()
    assert r["requested"] and client.get("/events").json()["events"] == []
    assert any("일정 추가 요청" in n["text"] for n in client.app.state.access.notifications("kjh"))
    mine = build_draft("내일 10시 개인 학습 정리 1시간 추가", TODAY, "yhr")
    assert client.post("/events", json={"user": "yhr", "draft": mine}).json()["event"]["event_id"] == "EVT-0001"
