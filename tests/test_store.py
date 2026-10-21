"""원장·정책 판단 로직 단위 테스트 — 임베딩·LLM 없이 SQLite만으로 돈다."""
# pylint: disable=missing-function-docstring,missing-class-docstring,redefined-outer-name,unused-argument  # pytest fixture·가짜 객체 관용
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger.store import LedgerStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = LedgerStore(tmp_path / "t.db")
    s.load_seed()
    return s


def test_superseded_decision_is_not_active(store):
    d3 = store.get_decision("OPS-0003")          # 롱폴링 — D-007로 대체됨
    assert d3["superseded_by"] == "OPS-0007"
    assert d3["is_active"] is False
    assert [d["decision_id"] for d in store.decisions(project_id="PRJ-OPSAI", topic="realtime.transport",
                                                      active_only=True)] == ["OPS-0007"]


def test_proposed_decision_has_no_authority(store):
    d10 = store.get_decision("OPS-0010")
    assert d10["status"] == "proposed" and d10["is_active"] is False


def test_approve_supersedes_previous_and_keeps_history(store):
    new_id = store.propose({"project_id": "PRJ-OPSAI", "topic": "perf.latency",
                            "statement": "최종 답변 기준 60초", "decided_by": "정한결",
                            "evidence": [{"meeting_id": "OPS-M05", "seq": 4, "quote": "최종 답변 기준을 60초로 늘리겠습니다."}]})
    assert store.get_decision(new_id)["is_active"] is False       # 승인 전에는 권위 없음
    out = store.approve(new_id, "정한결", "2026-10-20")
    assert out["superseded"] == ["OPS-0005"]
    assert store.get_decision("OPS-0005")["superseded_by"] == new_id   # 지우지 않고 대체 표시
    assert store.get_decision(new_id)["is_active"] is True


def test_only_current_policy_version_is_used(store):
    assert store.current_policy_version() == "v3"
    assert "PostgreSQL" in store.rule("ARC-01")["text"]               # 현행
    assert "MySQL" in store.rule("ARC-01", "v2")["text"]              # 구 정책은 조회만 가능


def test_waiver_respects_expiry_and_project(store):
    assert [w["waiver_id"] for w in store.active_waivers("SEC-01", "PRJ-OPSAI", "2026-10-13")] == ["W-001"]
    assert store.active_waivers("SEC-01", "PRJ-OPSAI", "2026-12-01") == []      # 만료 후
    assert store.active_waivers("ARC-02", "PRJ-OPSAI", "2025-06-01") == []      # 다른 프로젝트 예외
    # 예외가 있어도 정책 자체는 그대로
    assert "전송할 수 없다" in store.rule("SEC-01")["text"]


def test_next_decision_id(store):
    assert store.next_decision_id() == "OPS-0011"


def test_edit_proposed_replaces_alternatives_and_slots_and_returns_diff(store):
    new_id = store.propose({"project_id": "PRJ-OPSAI", "topic": "perf.latency", "statement": "최종 답변 기준 60초",
                            "decided_by": "정한결", "alternatives_rejected": ["90초"],
                            "slots": {"target": "최종 답변", "change": "30초 → 60초"}})
    out = store.update_proposed(new_id, rationale="사용자 체감 조사", alternatives=["45초", "120초"],
                                slots={"target": "최종 답변", "change": "30초 → 60초", "scope": "운영 화면"})
    assert out["before"]["alternatives_rejected"] == ["90초"]
    d = store.get_decision(new_id)
    assert d["alternatives_rejected"] == ["45초", "120초"]          # 기존 값이 있어도 새 값으로 바뀐다
    assert d["slots"]["scope"] == "운영 화면" and d["rationale"] == "사용자 체감 조사"
    assert d["statement"] == "최종 답변 기준 60초"                  # 안 넘긴 값은 그대로


def test_edit_preview_of_topic_change_shows_other_decisions_to_be_superseded(store):
    new_id = store.propose({"project_id": "PRJ-OPSAI", "topic": "perf.latency", "statement": "최종 답변 기준 60초",
                            "decided_by": "정한결"})
    assert [x["decision_id"] for x in store.will_supersede(new_id)] == ["OPS-0005"]
    moved = [x["decision_id"] for x in store.will_supersede(new_id, topic="realtime.transport")]
    assert moved == ["OPS-0007"]                                      # 주제를 바꾸면 대체 대상도 바뀐다


def test_approved_decision_cannot_be_edited(store):
    with pytest.raises(ValueError):
        store.update_proposed("OPS-0007", statement="바꿈")
