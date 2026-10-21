"""운영 중 주제·별칭 더하기 — 기본 파일은 그대로, store/vocab_extra.json에만 쌓이는지."""
# pylint: disable=missing-function-docstring,redefined-outer-name  # pytest fixture 관용
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import aliases, vocab  # noqa: E402
from ledger.store import LedgerStore  # noqa: E402


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_STORE", str(tmp_path))
    aliases.reload()
    yield tmp_path
    aliases.reload()


def test_new_topic_becomes_findable_by_alias(tmp_store):
    assert not [t for t in aliases.topics_of("장애 공지 채널은 뭘로 해?") if t["topic"] == "notice.channel"]
    before = aliases.version()
    vocab.add_topic("notice.channel", "장애 공지를 보내는 채널", ["장애 공지", "공지 채널", "공지 채널"])
    aliases.reload()
    assert "notice.channel" in vocab.topics()
    assert [t for t in aliases.topics_of("장애 공지 채널은 뭘로 해?") if t["topic"] == "notice.channel"]
    assert aliases.version() != before                        # 판 지문이 바뀌어 색인을 다시 만든다
    assert vocab.extra()["aliases"]["notice.channel"] == ["장애 공지", "공지 채널"]   # 중복 제거
    assert (tmp_store / "vocab_extra.json").exists()


@pytest.mark.parametrize("key,desc,al", [("Notice", "설명", ["공지"]),          # 대문자
                                         ("cache", "이미 있음", ["캐시 범위"]),  # 중복
                                         ("notice.channel", "", ["공지"]),     # 설명 없음
                                         ("notice.channel", "설명", ["", "x"])])  # 별칭 없음
def test_add_topic_rejects_bad_input(tmp_store, key, desc, al):
    with pytest.raises(ValueError):
        vocab.add_topic(key, desc, al)


def test_add_alias_to_existing_topic_skips_known(tmp_store):
    base = aliases.base_table()
    out = vocab.add_aliases("cache", ["상담원 알림", base["cache"][0]], base)
    assert out["added"] == ["상담원 알림"]
    with pytest.raises(ValueError):
        vocab.add_aliases("cache", [base["cache"][0]], base)


def test_decision_without_topic_cannot_be_approved_or_supersede(tmp_path):
    s = LedgerStore(tmp_path / "t.db")
    s.load_seed()
    a = s.propose({"project_id": "PRJ-OPSAI", "topic": "", "statement": "공지는 슬랙으로", "decided_by": "정한결"})
    assert s.will_supersede(a) == []                            # 빈 주제를 '전체'로 보지 않는다
    with pytest.raises(ValueError):
        s.approve(a, "정한결 팀장")
    assert s.get_decision(a)["status"] == "proposed"
