"""사실 질문(표 조회)과 근거 질문 후보 선택 단위 테스트 — LLM·MCP는 가짜."""
# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument  # 가짜 객체 관용
import asyncio

from agent import facts as F
from agent import retrieve as R
from agent import ledger_graph as G


def run(coro):
    return asyncio.run(coro)


DEC = {"decision_id": "OPS-0011", "statement": "캐시 적용 범위를 런북 문서로 제한", "status": "proposed",
       "decided_by": "서지후", "evidence": [{"meeting_id": "OPS-M04", "seq": 11}]}
M04 = {"meeting_id": "OPS-M04", "title": "기능 확장 회의", "date": "2026-10-13",
       "attendees": [{"name": "서지후", "role": "PL"}, {"name": "장상민", "role": "백엔드"}]}
M03 = {"meeting_id": "OPS-M03", "title": "구현 점검 회의", "date": "2026-10-06", "attendees": [{"name": "정한결", "role": "PM"}]}


class FactMCP:
    async def call(self, name, **a):
        if name == "decision_history":
            return {"decision": DEC if a["decision_id"] == "OPS-0011" else None,
                    "items": [{"kind": "open", "meeting_id": "OPS-M03", "statement": "캐시 대상 정의"}],
                    "events": [{"kind": "decision_proposed", "actor": "pdy", "at": "2026-10-20T10:00:00"}]}
        if name == "meeting_info":
            return {"OPS-M04": M04, "OPS-M03": M03}.get(a["meeting_id"], {})
        if name == "will_supersede":
            return []
        if name == "meeting_items":
            return [{"kind": "decision", "label": "확인 필요", "statement": "웹소켓 전환", "decision_id": None}]
        raise KeyError(name)


def test_fact_route_and_targets():
    assert F.is_fact_question("이거 언제 회의고 회의 참가자 누구야?")
    assert F.is_fact_question("이거 누가 승인한거야?")
    assert not F.is_fact_question("SSE는 왜 골랐어?")
    assert F.resolve_target("이거 누가 승인했어?", ["OPS-0011", "OPS-M04"]) == ("OPS-0011", "OPS-M04")
    assert F.resolve_target("OPS-M05 참석자?", ["OPS-0011"]) == ("", "OPS-M05")    # 메시지에 적힌 ID가 먼저


def test_fact_answer_from_tables_without_llm():
    text, refs = run(F.fact_answer(type("D", (), {"mcp": FactMCP()})(), "이거 누가 승인한거야?", ["OPS-0011"]))
    assert text.startswith("[승인 대기] **OPS-0011**")
    assert "아직 승인 전 · 승인 요청 pdy (2026-10-20)" in text and "승인할 사람: 팀장" in text
    assert "OPS-M04 기능 확장 회의 · 2026-10-13" in text and "서지후(PL) · 장상민(백엔드)" in text
    assert "불참: 정한결(팀장)" in text
    assert "이전 논의: OPS-M03 구현 점검 회의 · 2026-10-06 · 미결 '캐시 대상 정의'" in text
    assert refs == ["OPS-0011", "OPS-M04"]


def test_fact_without_target_asks_back():
    text, refs = run(F.fact_answer(type("D", (), {"mcp": FactMCP()})(), "참석자 누구야?", []))
    assert "어떤 결정이나 회의" in text and refs == []


def test_fact_mode_is_routed_without_llm():
    class NoLLM:
        async def json(self, *a, **k):
            raise AssertionError("사실 질문은 LLM을 부르지 않는다")
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=NoLLM(), quality_llm=NoLLM(), mcp=FactMCP()))
    st = run(graph.ainvoke({"message": "OPS-M04 참석자 누구야?", "events": []}))
    assert st["mode"] == "fact" and "서지후(PL)" in st["final"] and "결정 [확인 필요] 웹소켓 전환" in st["final"]


RES = {"authority": "decision", "current": {"decision_id": "OPS-0008", "topic": "session.store"}, "pending": [], "advisory": []}


def test_merge_agreement_and_disagreement():
    picked = {"decisions": [{"decision_id": "OPS-0008", "status": "approved", "active": True, "project_id": "PRJ-OPSAI"}],
              "opens": [], "shown": 5}
    res, disc, why = R.merge(RES, picked, "PRJ-OPSAI")
    assert res["current"]["decision_id"] == "OPS-0008" and not why and not disc
    other = {**picked, "decisions": [{**picked["decisions"][0], "decision_id": "OPS-0007"}]}
    res, _, why = R.merge(RES, other, "PRJ-OPSAI")
    assert res["current"]["decision_id"] == "OPS-0007" and "OPS-0008" in why[0]       # 어긋나면 확인 필요 사유
    res, _, why = R.merge(RES, {"decisions": [], "opens": [], "shown": 5}, "PRJ-OPSAI")
    assert res["current"]["decision_id"] == "OPS-0008" and "고르지 않음" in why[0]


def test_merge_open_items_become_discussion_and_labels():
    picked = {"decisions": [], "shown": 3, "opens": [{"item_id": "OPS-M03:open:10", "meeting_id": "OPS-M03", "statement": "캐시 대상",
                                                     "state": "open", "evidence": [{"meeting_id": "OPS-M03", "seq": 10,
                                                                                    "speaker": "서지후", "quote": "다음에 다시"}]}]}
    res, disc, why = R.merge({"authority": "none", "pending": [], "advisory": []}, picked, "PRJ-OPSAI")
    assert res["authority"] == "discussion" and disc[0]["seq"] == 10 and not why
    assert R.answer_label({"research": res, "discussion": disc}) == "미결"
    assert R.answer_label({"research": {"authority": "none"}}) == "찾지 못함"
    assert R.answer_label({"research": {"authority": "decision"}, "check_reasons": ["x"]}) == "확인 필요"


def test_how_many_days_policy_question_is_not_a_fact_lookup():
    assert not F.is_fact_question("대화 이력은 며칠 보관하기로 했어요?")   # 결정 내용을 묻는 질문
    assert F.is_fact_question("그거 며칠에 정했어?")


def test_merge_drops_same_topic_pending_that_was_not_picked():
    res = {"authority": "none", "advisory": [],
           "pending": [{"decision_id": "OPS-0032", "status": "proposed", "project_id": "PRJ-OPSAI"}]}
    out, _, _ = R.merge(res, {"decisions": [], "opens": [], "shown": 4}, "PRJ-OPSAI")
    assert out["pending"] == [] and out["pending_unpicked"][0]["decision_id"] == "OPS-0032"
    assert R.answer_label({"research": out}) == "찾지 못함"            # '아직 승인 대기'로 답하지 않는다
    kept, _, _ = R.merge(res, {"decisions": [res["pending"][0]], "opens": [], "shown": 4}, "PRJ-OPSAI")
    assert [p["decision_id"] for p in kept["pending"]] == ["OPS-0032"]


def test_who_proposed_is_a_fact_question():
    assert F.is_fact_question("이 아이디어 누가 제안했어?")
    assert F.is_fact_question("이거 처음 꺼낸 사람 누구야?")
