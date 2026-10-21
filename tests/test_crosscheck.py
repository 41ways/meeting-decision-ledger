"""교차검증(8b 판단 + 작은 모델 2개 × 질문 2가지) 단위 테스트 — 모델은 가짜."""
# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument  # 가짜 객체 관용
import asyncio

from agent import crosscheck as X
from agent import ledger_graph as G
from tests.test_graph import FakeLLM, FakeMCP, run


class Checker:
    """정방향 질문엔 fwd, 뒤집은 질문엔 inv로 답하는 가짜 작은 모델"""
    def __init__(self, fwd="yes", inv="no"):
        self.fwd, self.inv = fwd, inv

    async def json(self, system, user, schema, **kw):
        inverse = any(w in user for w in ("아직 정하지 않은", "어기거나", "다른 사안"))
        return {"answer": self.inv if inverse else self.fwd}, {"tokens": 1}


class Judge:
    def __init__(self, keep=True):
        self.keep = keep

    async def json(self, system, user, schema, **kw):
        return {"answer": "yes" if self.keep else "no"}, {"tokens": 1}


def deps(checkers, keep=True):
    return type("D", (), {"checkers": checkers, "quality_llm": Judge(keep)})()


def test_verdict_rules():
    assert X.verdict([True] * 4) == "keep"
    assert X.verdict([True, False, True, None]) == "rejudge"          # 기권은 이의로 세지 않는다
    assert X.verdict([False, False, True, True]) == "defer"


def test_votes_invert_the_second_question():
    agree, _ = run(X.votes([Checker(), Checker()], "재료", "결정인가?", "아직 정하지 않은 제안인가?"))
    assert agree == [True, True, True, True]
    agree, _ = run(X.votes([Checker("no", "yes"), Checker()], "재료", "결정인가?", "아직 정하지 않은 제안인가?"))
    assert agree == [False, False, True, True]


def test_check_outcomes():
    job = ("재료", "결정인가?", "아직 정하지 않은 제안인가?")
    assert run(X.check(deps([Checker(), Checker()]), "decision", *job))["outcome"] == "keep"
    one = [Checker(inv="yes"), Checker()]                                # 이의 1표 → 8b 재심
    assert run(X.check(deps(one, keep=True), "decision", *job))["outcome"] == "keep_after_rejudge"
    assert run(X.check(deps(one, keep=False), "decision", *job))["outcome"] == "defer"
    two = [Checker("no", "yes"), Checker()]                              # 이의 2표 → 확인 필요
    assert run(X.check(deps(two), "decision", *job))["outcome"] == "defer"


def test_disputed_decision_goes_to_human_not_deleted():
    """작은 모델 2표 이상이 '결정이 아니다'라고 하면 결정을 지우지 않고 확인 필요로 낮춘다."""
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=FakeMCP(),
                                              checkers=[Checker("no", "yes"), Checker("no", "yes")]))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    item = st["items"][0]
    assert item["review"]["status"] == "needs_approval" and item["label"] == "확인 필요"
    assert any("교차검증" in n for n in item["review"]["notes"])
    assert any(e["type"] == "CrossChecked" and e["kind"] == "decision" for e in st["events"])


def test_unanimous_agreement_keeps_result():
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=FakeMCP(),
                                              checkers=[Checker(), Checker()]))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    assert st["items"][0]["review"]["status"] == "change_approved"        # 교차검증 없이 낸 결과와 같다
