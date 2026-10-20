"""그래프 단위 테스트 — LLM·MCP를 가짜로 갈아 끼운다 (Wave 13 의존성 주입 덕분에 가능).

실모델 없이 '코드가 책임지는 부분'만 검증한다: 결정 판별 규칙, 인용 채우기, 판정표, 검증 노드.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring,redefined-outer-name,unused-argument  # pytest fixture·가짜 객체 관용
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


def test_topic_key_strips_description_and_new_prefix():
    assert G.topic_key("session.store: 대화 세션 저장소") == "session.store"
    assert G.topic_key("new:agent.gate") == "agent.gate"
    assert G.topic_key(None) == ""


def test_gate_requester_is_not_owner():
    st = {**_gate_state([], [
        {"seq": 12, "statement": "로그 마스킹 스크립트 작성", "owner": "정한결", "due": None},
    ]), "meeting_date": "2026-10-20", "segments": [
        {"meeting_id": "OPS-M05", "date": "2026-10-20", "seq": 12, "ts": "", "speaker": "정한결", "role": "PM",
         "text": "누가 로그 마스킹 스크립트 좀 만들어 주세요. 급해요."},
    ]}
    assert run(G.gate_node(st, deps=None))["action_items"][0]["owner"] is None


def test_gate_takes_corrected_due_from_later_utterance():
    def seg(q, sp, role, t):
        return {"meeting_id": "OPS-M03", "date": "2026-10-06", "seq": q, "ts": "",
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
            checks = [{"ref": "OPS-0007", "relation": "different", "reason": "SSE와 다름"}]
            return {"checks": checks, "waivers": []}, {"tokens": 1}
        return {}, {"tokens": 1}

    async def text(self, system, user, on_token=None, **kw):
        return "안녕하세요", {"tokens": 1}


class FakeMCP:
    calls = 0

    async def call(self, name, **a):
        self.saved = getattr(self, "saved", [])
        if name == "save_meeting_record":
            self.saved.append(a)
            return {"saved": len(a["items"]), "linked": []}
        return {"get_segments": SEGS,
                "resolve_current": {**RES_WS, "rules": [], "advisory": [], "notes": []},
                "check_authority": {"ok": True, "rule": "AUTH-01@v3"},
                "check_waiver": {"active_waivers": []},
                "meeting_info": {"meeting_id": "OPS-M04", "date": "2026-10-13", "title": "기능 확장 회의",
                                 "attendees": [{"name": "서지후", "role": "PL"}]},
                "list_decisions": [], "open_items": [],
                "topic_candidates": [{"topic": "cache", "score": 0.8}]}[name]

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


class BrokenLookupMCP(FakeMCP):
    async def call(self, name, **a):
        if name == "resolve_current":
            raise RuntimeError("chroma 연결 끊김")
        return await super().call(name, **a)


def test_lookup_failure_is_not_reported_as_ok():
    """기준 조회가 실패하면 '문제없음'이 아니라 '승인 필요'로 사람에게 돌린다 (조용한 실패 방지)."""
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=BrokenLookupMCP()))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    rv = st["items"][0]["review"]
    assert rv["status"] == "needs_approval" and "기준 조회 실패" in rv["notes"][0]


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
                return [{"meeting_id": "OPS-M04", "date": "2026-10-13", "title": "기능 확장 회의",
                         "attendees": ["서지후"], "domain": "meeting"},
                        {"meeting_id": "OPS-M05", "date": "2026-10-20", "title": "현업 피드백 회의",
                         "attendees": ["정한결"], "domain": "meeting"},
                        {"meeting_id": "HBE-I01", "date": "2026-10-16", "title": "면접",
                         "attendees": ["서지후", "지원자 K"], "domain": "interview"}]
            return await super().call(name, **a)
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=SchedMCP(),
                                              today="2026-10-20"))
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


def test_user_scope_project_overrides_default():
    seen = {}

    class ScopeMCP(FakeMCP):
        async def call(self, name, **a):
            if name == "resolve_current":
                seen["project_id"] = a.get("project_id")
                return {"topic": "cache", "authority": "none", "current": None, "rules": [], "baseline": [],
                        "pending": [], "advisory": [], "notes": []}
            if name == "search_transcript":
                seen["meeting_id"] = a.get("meeting_id")
                return []
            return await super().call(name, **a)

    class TopicLLM(FakeLLM):
        async def json(self, system, user, schema, **kw):
            if "topic" in schema["properties"]:
                return {"topic": "cache", "project": "PRJ-OPSAI"}, {"tokens": 1}
            return await super().json(system, user, schema, **kw)

    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=TopicLLM(), quality_llm=TopicLLM(), mcp=ScopeMCP()))
    st = run(graph.ainvoke({"message": "캐시는 어떻게 하기로 했어?", "events": [], "scope_project": "CSB",
                            "scope_meeting": "OPS-M03"}))
    plan = next(e for e in st["events"] if e["type"] == "PlanCreated")
    assert plan["project"] == "CSB" and plan["how"] == "직접 지정"
    assert seen["project_id"] == "PRJ-CSBOT" and seen["meeting_id"] == "OPS-M03"
    st = run(graph.ainvoke({"message": "캐시는 어떻게 하기로 했어?", "events": []}))
    assert next(e for e in st["events"] if e["type"] == "PlanCreated")["how"].startswith("기본값")


def test_answer_judge_low_score_triggers_rewrite():
    st = {"mode": "question", "message": "SSE는 왜 골랐어?", "draft": "OPS-0007 때문입니다.", "events": [],
          "answer_sources": "OPS-0007 ...",
          "items": [{"research": {"authority": "decision", "current": {"decision_id": "OPS-0007"}, "pending": []}}]}

    class LowJudge(FakeLLM):
        async def json(self, system, user, schema, **kw):
            score = {"relevance": 2, "grounding": 5, "completeness": 4, "clarity": 4, "feedback": "질문에 바로 답하지 않음"}
            return score, {"tokens": 1}

    out = run(G.validate_node(st, deps=G.LedgerDeps(fast_llm=LowJudge(), quality_llm=LowJudge(), mcp=FakeMCP())))
    assert out["validation_feedback"] and "품질 심판" in out["validation_feedback"]
    assert out["events"][-1]["judge"]["scores"]["relevance"] == 2


def test_missed_decision_candidate_is_rechecked():
    class RecheckLLM(FakeLLM):
        async def json(self, system, user, schema, **kw):
            props = schema["properties"]
            if "decisions" in props:      # 1차 추출: [4] 결정 후보를 빠뜨림
                return {"overview": {"topic": "t", "discussion": [], "direction": ""}, "decisions": [],
                        "action_items": [], "undecided": []}, {"tokens": 1}
            if "items" in props and "is_decision" in str(schema):
                return {"items": [{"seq": 4, "is_decision": True, "statement": "실시간 전송을 WebSocket으로 변경",
                                   "topic": "realtime.transport", "proposal_seq": 3}]}, {"tokens": 1}
            return await super().json(system, user, schema, **kw)

    st = {"message": "M04 회의 정리해줘", "mode": "meeting", "meeting_id": "OPS-M04", "events": []}
    out = run(G.plan_node(st, deps=G.LedgerDeps(fast_llm=RecheckLLM(), quality_llm=RecheckLLM(), mcp=FakeMCP())))
    assert [d["decided_seq"] for d in out["items"]] == [4]
    assert out["events"][-1]["rechecked"] >= 1


# ---------------------------------------------------------------- '모르면 모른다' 가드
def test_no_basis_is_not_reported_as_ok():
    """비교할 기준이 하나도 없으면 '문제없음'이 아니라 '비교할 기준 없음'."""
    res = {"topic": "cache", "authority": "none", "current": None, "rules": [], "pending": [], "advisory": [],
           "baseline": []}
    assert G.decide(_item(res), [], {})["status"] == "no_basis"


class BrokenJudgeLLM(FakeLLM):
    async def json(self, system, user, schema, **kw):
        if "checks" in schema["properties"]:
            return {}, {"tokens": 1, "parse_error": True}      # 출력이 잘려 JSON 해석 실패
        return await super().json(system, user, schema, **kw)


def test_unparseable_judge_output_goes_to_human():
    """판정 LLM 출력이 깨지면 판정표가 '문제없음'을 내지 않고 사람에게 넘긴다."""
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=BrokenJudgeLLM(), mcp=FakeMCP()))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    rv = st["items"][0]["review"]
    assert rv["status"] == "needs_approval" and "판정 실패" in rv["notes"][0]


def test_topic_doubt_turns_decision_conflict_into_check():
    """주제가 의심스러우면 결정 비교 충돌로 단정하지 않는다. 정책 충돌은 그대로."""
    review = {"status": "conflict", "conflicts": [{"type": "past_decision", "against": ["OPS-0008"], "detail": ""}],
              "changes": [], "notes": []}
    doubt = {"research": {"topic_doubt": True, "notes": []}}
    assert G._guard_topic(doubt, review)["status"] == "needs_approval"
    policy = {**review, "conflicts": [{"type": "policy", "against": ["SEC-01"], "detail": ""}]}
    assert G._guard_topic(doubt, policy)["status"] == "conflict"
    other = {"research": {"similar_other": [{"decision_id": "OPS-0008"}], "notes": []}}
    assert G._guard_topic(other, {**review, "status": "ok", "conflicts": []})["status"] == "needs_approval"
    assert G._guard_topic({"research": {}}, review) is review          # 의심이 없으면 그대로


class LowScoreMCP(FakeMCP):
    async def call(self, name, **a):
        if name == "topic_candidates":
            return [{"topic": "session.store", "score": 0.41}]       # 새 주제(함정) 수준의 점수
        return await super().call(name, **a)


def test_low_score_topic_is_not_silently_filled():
    """LLM이 주제를 못 정했을 때 점수 낮은 검색 1순위를 억지로 붙이지 않는다 (조용한 폴백 방지)."""
    deps = G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=LowScoreMCP())
    assert run(G.ensure_topic(deps, "", "회식 장소를 정하자")) == ""
    assert run(G.ensure_topic(deps, "cache", "캐시")) == "cache"


def _q_state(draft, research, discussion=None, attempts=0):
    item = {"research": research, "project": G.DEFAULT_PID, **({"discussion": discussion} if discussion else {})}
    return {"mode": "question", "draft": draft, "events": [], "items": [item], "validation_attempts": attempts,
            "answer_sources": "[OPS-0005] 응답 시간 기준을 첫 진행 표시 2초 이내, 최종 답변 30초 이내로 변경한다."}


RES_LAT = {"authority": "decision", "pending": [],
           "current": {"decision_id": "OPS-0005", "statement": "응답 시간 기준을 첫 진행 표시 2초, 최종 30초로 변경"}}


def test_value_not_in_sources_is_rejected_then_replaced_by_ledger_answer():
    """근거에 없는 숫자를 쓰면 반려하고, 3번째에도 남으면 모델 문장을 원장 기록으로 바꾼다."""
    out = run(G.validate_node(_q_state("OPS-0005에 따라 최종 답변은 45초 이내입니다.", RES_LAT), deps=None))
    assert "근거에 없는 값: 45초" in out["validation_feedback"]
    out = run(G.validate_node(_q_state("OPS-0005에 따라 최종 답변은 45초 이내입니다.\n\n### OPS-0005 · x", RES_LAT,
                                       attempts=2), deps=None))
    assert out["validation_feedback"] is None and "45초" not in out["draft"]
    assert out["draft"].startswith("[확정] 현재 유효한 결정은 OPS-0005입니다") and "### OPS-0005" in out["draft"]
    ok = run(G.validate_node(_q_state("OPS-0005에 따라 최종 답변은 30초 이내입니다.", RES_LAT, attempts=2), deps=None))
    assert ok["validation_feedback"] is None and "draft" not in ok          # 근거와 맞으면 모델 답 그대로


def test_nothing_found_must_be_said_not_undecided():
    """관련 논의가 없으면 '결정되지 않음'(논의는 있었다는 뜻)이 아니라 '찾지 못했다'고 해야 한다."""
    none = {"authority": "none", "pending": []}
    out = run(G.validate_node(_q_state("논의는 있었지만 결정되지 않았습니다.", none), deps=None))
    assert "찾지 못" in out["validation_feedback"]
    disc = [{"meeting_id": "OPS-M03", "seq": 10, "speaker": "서지후", "text": "캐시는 다음에 다시"}]
    out = run(G.validate_node(_q_state("관련 결정을 찾지 못했습니다.", {"authority": "discussion", "pending": []}, disc),
                              deps=None))
    assert "결정되지 않" in out["validation_feedback"]


def test_meeting_result_is_saved_to_records_with_labels():
    """회의 정리 결과(결정 후보·할 일)가 라벨과 함께 회의 기록에 저장되고, 답이 가리킨 대상(refs)이 남는다."""
    mcp = FakeMCP()
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=mcp))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    saved = mcp.saved[0]
    kinds = {i["kind"] for i in saved["items"]}
    assert saved["meeting_id"] == "OPS-M04" and kinds == {"decision", "action"}
    assert saved["items"][0]["label"] == "승인 대기" and "OPS-M04" in st["report"]["refs"]
    assert "참석 서지후(PL)" in st["final"]


class OpinionLLM(FakeLLM):
    async def json(self, system, user, schema, **kw):
        if "mode" in schema["properties"]:
            return {"mode": "opinion"}, {"tokens": 1}
        if "topic" in schema["properties"] and "project" in schema["properties"]:
            return {"topic": "realtime.transport", "project": "PRJ-OPSAI"}, {"tokens": 1}
        if "items" in schema["properties"] and "alternative" in str(schema):
            return {"items": [{"i": 0, "alternative": "SSE 유지, 멈춤은 별도 요청"}]}, {"tokens": 1}
        return await super().json(system, user, schema, **kw)


def test_opinion_conflict_gets_suggestions_without_evidence():
    """의견 검토 항목은 근거 발언이 없어도 해결 제안·AI 대안이 붙는다 (평가 중 멈춘 회귀 방지)."""
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=OpinionLLM(), quality_llm=OpinionLLM(), mcp=FakeMCP()))
    st = run(graph.ainvoke({"message": "실시간 전송을 다시 롱폴링으로 돌리면 어떨까요?", "events": []}))
    item = st["items"][0]
    assert item["review"]["status"] == "conflict" and item["label"] == "확인 필요"
    assert any(s["ai"] for s in item["suggestions"]) and "해결 방향" in st["final"]
