"""해석기(Resolver) 단위 테스트 — 벡터 검색을 가짜로 바꿔 임계값·교차 확인만 본다."""
# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument  # 가짜 객체 관용
import pytest

from ledger.resolve import Resolver
from ledger.store import LedgerStore


class FakeRAG:
    def __init__(self, transcript=(), decisions=(), baseline=(), support=None):
        self.transcript, self.dec, self.base, self.support = list(transcript), list(decisions), list(baseline), support

    def search_transcript(self, query, meeting_id=None, project_id=None, k=5, min_score=None):
        return [r for r in self.transcript if min_score is None or r["_score"] >= min_score][:k]

    def search_decisions(self, query, project_id=None, k=5, filtered=True, include_proposed=False):
        return self.dec[:k]

    def search_baseline(self, query, project_id=None, k=4):
        return self.base[:k]

    def search_rules(self, query, k=4, filtered=True, domain=None):
        return []

    def topic_support(self, query, topic):
        return self.support


@pytest.fixture
def store(tmp_path):
    s = LedgerStore(tmp_path / "ledger.db")
    s.load_seed()
    return s


def test_unrelated_utterances_are_not_reported_as_discussion(store):
    """논의한 적 없는 주제: 가까운 발언이 있어도 하한 미만이면 '논의 있음'이 아니라 '찾지 못함'."""
    rag = FakeRAG(transcript=[{"meeting_id": "OPS-M05", "seq": 5, "_score": 0.52}])
    res = Resolver(store, rag).resolve("메신저를 팀즈로 바꾸기로 했나요?", topic="")
    assert res.authority == "none" and not res.discussion and "찾지 못함" in res.notes[-1]
    rag.transcript = [{"meeting_id": "OPS-M03", "seq": 10, "_score": 0.71}]
    assert Resolver(store, rag).resolve("Redis 캐시 쓰기로 했어요?", topic="").authority == "discussion"


def test_low_score_topic_is_left_empty(store):
    rag = FakeRAG(decisions=[{"topic": "session.store", "_score": 0.44, "decision_id": "OPS-0008"}])
    assert Resolver(store, rag).topic_of("회식 장소를 정하자") == ""
    rag.dec = [{"topic": "session.store", "_score": 0.81, "decision_id": "OPS-0008"}]
    assert Resolver(store, rag).topic_of("대화 저장을 Redis로") == "session.store"


def test_cross_check_flags_doubtful_topic_and_similar_other_decision(store):
    """주제 자료와 멀면 topic_doubt, 다른 주제 유효 결정이 더 가까우면 similar_other."""
    rag = FakeRAG(support=0.42)
    assert Resolver(store, rag).resolve("x", topic="realtime.transport").topic_doubt
    rag = FakeRAG(support=None, decisions=[{"decision_id": "OPS-0008", "topic": "session.store",
                                            "statement": "대화 이력은 PostgreSQL", "_score": 0.60}])
    res = Resolver(store, rag).resolve("Redis 캐시 쓰기로 했어요?", topic="cache")
    assert [d["decision_id"] for d in res.similar_other] == ["OPS-0008"] and not res.topic_doubt


def test_decision_id_in_text_is_looked_up_directly(store):
    res = Resolver(store, FakeRAG()).resolve("OPS-0007은 왜 정했어요?", topic="")
    assert [d["decision_id"] for d in res.id_refs] == ["OPS-0007"]
