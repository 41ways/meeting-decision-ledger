"""그래프 단위 테스트 — LLM·MCP를 가짜로 갈아 끼운다 (Wave 13 의존성 주입 덕분에 가능).

실모델 없이 '코드가 책임지는 부분'만 검증한다: 결정 판별 규칙, 인용 채우기, 판정표, 검증 노드.
"""
import asyncio

import pytest

from agent import ledger_graph as G

SEGS = [
    {"meeting_id": "OPS-M04", "date": "2026-10-13", "seq": 3, "ts": "", "speaker": "장상민", "role": "백엔드",
     "text": "실시간 전송을 웹소켓으로 바꾸는 게 깔끔합니다."},
    {"meeting_id": "OPS-M04", "date": "2026-10-13", "seq": 4, "ts": "", "speaker": "서지후", "role": "PL",
     "text": "그럼 실시간 전송은 웹소켓으로 바꾸는 걸로 하죠."},
    {"meeting_id": "OPS-M04", "date": "2026-10-13", "seq": 5, "ts": "", "speaker": "노영오", "role": "AI 개발",
     "text": "예전에 롱폴링 쓰다가 SSE로 바꾸고 나서 요청 수는 확실히 줄었죠."},
    {"meeting_id": "OPS-M04", "date": "2026-10-13", "seq": 12, "ts": "", "speaker": "서지후", "role": "PL",
     "text": "상민 님은 웹소켓 전환 설계를 10월 17일까지 해 주세요."},
    {"meeting_id": "OPS-M04", "date": "2026-10-13", "seq": 13, "ts": "", "speaker": "장상민", "role": "백엔드",
     "text": "네 17일까지요."},
]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 결정 판별 규칙
@pytest.mark.parametrize("text,role,expected", [
    ("그럼 웹소켓으로 바꾸는 걸로 하죠.", "PL", True),
    ("최종 답변 기준을 60초로 늘리겠습니다.", "PM", True),
    ("그건 권위 게이트로 코드에서 걸러요.", "PL", True),            # 진행자의 단정 발언
    ("예전에 롱폴링 쓰다가 SSE로 바꾸고 나서 줄었죠.", "AI 개발", False),  # 회상
    ("결과 공유해 주시면 보고 정할게요. 아직 확정은 아닙니다.", "PL", False),  # 보류
])
def test_is_decision_utterance(text, role, expected):
    assert G.is_decision_utterance({"text": text, "role": role}) is expected


# ---------------------------------------------------------------- gate
def _gate_state(items, actions=()):
    return {"mode": "meeting", "segments": SEGS, "meeting_date": "2026-10-13", "items": list(items),
            "action_items": list(actions), "undecided": [], "events": []}


def test_gate_fills_quote_from_source_and_drops_recall():
    out = run(G.gate_node(_gate_state([
        {"decided_seq": 4, "proposal_seq": 3, "statement": "WebSocket으로 변경", "topic": "realtime.transport"},
        {"decided_seq": 5, "proposal_seq": 5, "statement": "SSE로 변경", "topic": "realtime.transport"},  # 회상 → 제거
    ]), deps=None))
    assert [d["decided_seq"] for d in out["items"]] == [4]
    ev = out["items"][0]["evidence"]
    assert [e["seq"] for e in ev] == [3, 4]
    assert ev[1]["quote"] == SEGS[1]["text"]          # 인용은 모델이 아니라 원문에서
    assert out["items"][0]["decided_by_role"] == "PL"


def test_gate_clears_invented_owner_and_due():
    out = run(G.gate_node(_gate_state([], [
        {"seq": 12, "statement": "웹소켓 전환 설계", "owner": "장상민(백엔드)", "due": "2023-10-17"},
        {"seq": 12, "statement": "다른 일", "owner": "김태림", "due": "2026-10-30"},       # 원문에 없는 담당·기한
    ]), deps=None))
    a, b = out["action_items"]
    assert a["owner"] == "장상민" and a["due"] == "2026-10-17"     # 괄호 제거 + 연도 보정
    assert [e["seq"] for e in a["evidence"]] == [12, 13]
    assert b["owner"] is None and b["due"] is None


def test_gate_takes_corrected_due_from_later_utterance():
    seg = lambda q, sp, role, t: {"meeting_id": "OPS-M03", "date": "2026-10-06", "seq": q, "ts": "",
                                  "speaker": sp, "role": role, "text": t}
    st = {**_gate_state([], [
        {"seq": 14, "statement": "캐시 후보 정리", "owner": "장상민", "due": "2026-10-10"},
        {"seq": 14, "statement": "로그 수집", "owner": "장상민", "due": "2026-10-10"},   # 정정 발언과 무관
    ]), "meeting_date": "2026-10-06", "segments": [
        seg(14, "장상민", "백엔드", "네 10일까지 로그 수집 하고, 캐시 후보 정리도 같이 할게요."),
        seg(15, "정한결", "PM", "캐시 후보는 10월 13일 회의 전까지로 할게요."),
    ]}
    a, b = run(G.gate_node(st, deps=None))["action_items"]
    assert a["due"] == "2026-10-13" and [e["seq"] for e in a["evidence"]] == [14, 15]
    assert b["due"] == "2026-10-10"


# ---------------------------------------------------------------- 판정표 decide()
def _item(research, auth=None, waivers=()):
    return {"statement": "x", "topic": "t", "research": research, "authority_check": auth, "waivers": list(waivers)}


RES_WS = {"topic": "realtime.transport", "authority": "decision",
          "current": {"decision_id": "OPS-0007", "statement": "SSE로 변경"},
          "rules": [{"rule_id": "ARC-02", "policy_version": "v3", "text": "WebSocket은 예외 승인 필요"}],
          "baseline": [], "pending": [], "advisory": [{"decision_id": "CSB-0004", "project_id": "PRJ-CSBOT",
                                                       "statement": "WebSocket 중단"}]}


def test_decide_policy_decision_and_advisory():
    r = G.decide(_item(RES_WS, auth={"ok": True, "rule": "AUTH-01@v3"}), [
        {"ref": "ARC-02", "relation": "different", "reason": "예외 승인 없음"},
        {"ref": "OPS-0007", "relation": "different", "reason": "SSE와 다름"},
        {"ref": "OPS-0007", "relation": "same", "reason": "중복 답변은 무시"},
        {"ref": "OPS-0003", "relation": "different", "reason": "대체된 결정 — 목록에 없으니 무시"},
        {"ref": "CSB-0004", "relation": "different", "reason": "참고"}], {})
    types = {(c["type"], c["against"][0]) for c in r["conflicts"]}
    assert types == {("policy", "ARC-02"), ("past_decision", "OPS-0007"), ("past_project", "CSB-0004")}
    assert r["status"] == "conflict"


def test_decide_authority_added_by_code_even_if_llm_says_same():
    res = {"topic": "schedule", "authority": "baseline", "current": None, "rules": [], "pending": [], "advisory": [],
           "baseline": [{"doc_id": "REQ", "doc_version": "v1.1", "section": "REQ-06 일정", "text": "12/15 반영"}]}
    r = G.decide(_item(res, auth={"ok": False, "rule": "AUTH-03@v3", "approver": "PM / 본부장", "reason": "2주 이상은 본부장"}),
                 [{"ref": "REQ-06", "relation": "same", "reason": "모델이 놓침"}], {})
    assert r["status"] == "conflict" and r["conflicts"][0]["type"] == "authority"


def test_decide_change_approved_when_authorized():
    res = {"topic": "perf.latency", "authority": "decision", "current": {"decision_id": "OPS-0005", "statement": "30초"},
           "rules": [], "pending": [], "advisory": [],
           "baseline": [{"doc_id": "REQ", "doc_version": "v1.1", "section": "REQ-03 응답", "text": "30초"}]}
    r = G.decide(_item(res, auth={"ok": True, "rule": "AUTH-02@v3"}),
                 [{"ref": "OPS-0005", "relation": "different", "reason": "60초"},
                  {"ref": "REQ-03", "relation": "different", "reason": "60초"}], {})
    assert r["status"] == "change_approved" and set(r["changes"]) == {"OPS-0005", "REQ-03"}


def test_decide_waiver_only_when_scope_covers():
    res = {"topic": "llm.external", "authority": "none", "current": None, "baseline": [], "pending": [], "advisory": [],
           "rules": [{"rule_id": "SEC-01", "policy_version": "v3", "text": "원문 외부 전송 금지"}]}
    w = [{"waiver_id": "W-001", "rule_id": "SEC-01", "scope": "마스킹 샘플", "expires_at": "2026-11-30"}]
    checks = [{"ref": "SEC-01", "relation": "different", "reason": "외부 전송"}]
    assert G.decide(_item(res, waivers=w), checks, {"W-001": True})["waiver_applied"] == "W-001"
    r = G.decide(_item(res, waivers=w), checks, {"W-001": False})
    assert r["status"] == "conflict" and r["waiver_applied"] is None


def test_decide_pending_confirmation():
    res = {"topic": "llm.tiering", "authority": "none", "current": None, "baseline": [], "rules": [], "advisory": [],
           "pending": [{"decision_id": "OPS-0010", "statement": "모델 티어링"}]}
    r = G.decide(_item(res), [{"ref": "OPS-0010", "relation": "same", "reason": "같은 내용"}], {})
    assert r["status"] == "ok" and r["confirms"] == ["OPS-0010"]


# ---------------------------------------------------------------- validate
def test_validate_question_requires_decision_id_then_gives_up_after_3():
    st = {"mode": "question", "draft": "SSE로 정했습니다.", "events": [],
          "items": [{"research": {"authority": "decision", "current": {"decision_id": "OPS-0007"}, "pending": []}}]}
    out = run(G.validate_node(st, deps=None))
    assert out["validation_feedback"] and "OPS-0007" in out["validation_feedback"]
    out = run(G.validate_node({**st, "validation_attempts": 2}, deps=None))
    assert out["validation_feedback"] is None                      # 무한 재작성 방지


# ---------------------------------------------------------------- 그래프 전체 (가짜 LLM·MCP)
class FakeLLM:
    name = "fake"

    async def json(self, system, user, schema, **kw):
        props = schema["properties"]
        if "mode" in props:
            return {"mode": "chat"}, {"tokens": 1}
        if "decisions" in props:
            return {"decisions": [{"seq": 4, "proposal_seq": 3, "statement": "실시간 전송을 WebSocket으로 변경",
                                   "topic": "realtime.transport"}],
                    "action_items": [{"seq": 12, "statement": "웹소켓 전환 설계", "owner": "장상민", "due": "2026-10-17"}],
                    "undecided": []}, {"tokens": 1}
        if "checks" in props:
            return {"checks": [{"ref": "OPS-0007", "relation": "different", "reason": "SSE와 다름"}], "waivers": []}, {"tokens": 1}
        return {}, {"tokens": 1}

    async def text(self, system, user, on_token=None):
        return "안녕하세요", {"tokens": 1}


class FakeMCP:
    calls = 0

    async def call(self, name, **a):
        return {"get_segments": SEGS,
                "resolve_current": {**RES_WS, "rules": [], "advisory": [], "notes": []},
                "check_authority": {"ok": True, "rule": "AUTH-01@v3"},
                "check_waiver": {"active_waivers": []}}[name]

    async def prompt(self, name, **a):
        return "절차"

    async def resource(self, uri):
        return "realtime.transport: 실시간 전송"


def test_graph_meeting_path_with_fakes():
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=FakeMCP()))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    route = [e["type"] for e in st["events"]]
    assert route[:3] == ["RouteDecided", "PlanCreated", "GateApplied"]
    # PL에게 기술 결정 권한(AUTH-01)이 있고 정책 충돌이 없으니 '기준 변경(승인됨)'
    assert st["items"][0]["review"]["changes"] == ["OPS-0007"]
    assert "[4] 서지후" in st["final"] and "기준 변경" in st["final"]


class EmptyMCP(FakeMCP):
    async def call(self, name, **a):
        return [] if name == "get_segments" else await super().call(name, **a)


def test_graph_missing_meeting_does_not_crash():
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=EmptyMCP()))
    st = run(graph.ainvoke({"message": "OPS-M09 회의 정리해줘", "events": []}))
    assert "원문이 없습니다" in st["final"]
    assert "gate" not in [e.get("node") for e in st["events"]] and st["events"][-1]["type"] == "Finalize"


def test_schedule_question_answered_from_meeting_list():
    class SchedMCP(FakeMCP):
        async def call(self, name, **a):
            if name == "list_meetings":
                return [{"meeting_id": "OPS-M04", "date": "2026-10-13", "title": "기능 확장 회의", "attendees": ["서지후"], "domain": "meeting"},
                        {"meeting_id": "OPS-M05", "date": "2026-10-20", "title": "현업 피드백 회의", "attendees": ["정한결"], "domain": "meeting"},
                        {"meeting_id": "HBE-I01", "date": "2026-10-16", "title": "면접", "attendees": ["서지후", "지원자 K"], "domain": "interview"}]
            return await super().call(name, **a)
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=SchedMCP(), today="2026-10-20"))
    st = run(graph.ainvoke({"message": "이번 주 회의 몇개야?", "events": []}))
    assert st["mode"] == "schedule" and st["final"].startswith("이번 주(10-19~10-25) 회의는 1건")
    for q in ("지난주 회의 몇 개야?", "저번주 회의 몇개야", "저번 주 회의 몇 개야?", "전주 회의 몇 건이야"):
        st = run(graph.ainvoke({"message": q, "events": []}))
        assert st["final"].startswith("지난주(10-12~10-18) 회의는 1건") and "OPS-M04" in st["final"], q
    st = run(graph.ainvoke({"message": "지난주 일정은 몇개야", "events": []}))
    assert st["final"].startswith("지난주(10-12~10-18) 일정은 2건입니다 (회의 1건, 면접 1건)"), st["final"]
    for q in ("지지난주 회의 몇 개야?", "저저번주 회의 몇개", "전전주 회의 몇 건", "2주 전 회의 몇 개야"):
        st = run(graph.ainvoke({"message": q, "events": []}))
        assert "(10-05~10-11)" in st["final"], (q, st["final"])
