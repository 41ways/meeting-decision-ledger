"""권한·요청·알림 단위 테스트 (임시 SQLite)."""
import pytest

from ledger.access import AccessStore

M04 = {"meeting_id": "OPS-M04", "attendees": ["서지후", "장상민", "노영오", "한소율"]}
DEC = {"decision_id": "OPS-0011", "evidence": [{"meeting_id": "OPS-M04"}]}
OLD = {"decision_id": "CSB-0004", "evidence": [{"meeting_id": "CSB-M09"}]}     # 원문 파기된 과거 결정


@pytest.fixture
def acc(tmp_path):
    return AccessStore(tmp_path / "access.db")


def test_default_is_attendees_only(acc):
    assert acc.can_view_meeting("pdy", M04)          # 참석자
    assert acc.can_view_meeting("kjh", M04)          # 팀장은 항상
    assert not acc.can_view_meeting("yhr", M04)      # 인턴, 불참
    assert not acc.can_view_meeting("jmh", M04)      # 팀원, 불참


def test_role_grant_by_admin(acc):
    acc.set_acl("OPS-M04", "팀원", True, "kjh")
    assert acc.can_view_meeting("jmh", M04) and not acc.can_view_meeting("yhr", M04)
    acc.set_acl("OPS-M04", "팀원", False, "kjh")
    assert not acc.can_view_meeting("jmh", M04)


def test_view_request_scopes_and_notifications(acc):
    rid = acc.request_access("yhr", "OPS-0011", "온보딩")
    assert any("열람을 요청" in n["text"] for n in acc.notifications("kjh"))
    assert not acc.can_view_decision("yhr", DEC, {"OPS-M04": M04})
    acc.decide_request(rid, "kjh", True, "user")
    assert acc.can_view_decision("yhr", DEC, {"OPS-M04": M04})
    assert acc.notifications("yhr")[0]["kind"] == "approved"
    assert "승인했습니다" in acc.notifications("kjh")[0]["text"]


def test_rejected_request_notifies_requester(acc):
    rid = acc.request_access("lsj", "OPS-M05", "")
    acc.decide_request(rid, "kjh", False)
    assert acc.notifications("lsj")[0]["kind"] == "rejected"


def test_role_scope_applies_to_same_role(acc):
    rid = acc.request_access("jmh", "OPS-M04", "")
    acc.decide_request(rid, "kjh", True, "role")
    assert acc.can_view_meeting("hsy", {**M04, "attendees": []})    # 같은 팀원
    assert not acc.can_view_meeting("yhr", {**M04, "attendees": []})  # 인턴은 아님


def test_deleted_transcript_decision_needs_manager_level(acc):
    assert acc.can_view_decision("pdy", OLD, {})      # 담당자
    assert not acc.can_view_decision("lsj", OLD, {})  # 팀원


def test_proposal_flow_notifies_both_sides(acc):
    acc.record_proposal("OPS-0011", "pdy", "OPS-M04")
    assert any("승인 요청" in n["text"] for n in acc.notifications("kjh"))
    acc.after_decision("OPS-0011", "kjh", True, ["OPS-0005"])
    assert "대체: OPS-0005" in acc.notifications("kjh")[0]["text"]
    assert acc.notifications("pdy")[0]["kind"] == "approved"
    acc.record_proposal("OPS-0012", "pdy", "의견")
    acc.after_decision("OPS-0012", "kjh", False, [], "본부장 승인 필요")
    assert "반려" in acc.notifications("pdy")[0]["text"]


def test_threads_latest_first(acc):
    acc.touch_thread("a", "kjh", "첫 대화", "OPS", "OPS-M04")
    acc.touch_thread("b", "kjh", "둘째", "HBE", None)
    acc.conn.execute("UPDATE threads SET updated='2026-01-01' WHERE thread_id='a'")
    assert [t["thread_id"] for t in acc.threads("kjh")] == ["b", "a"]


def test_answer_cache_reused_only_when_inputs_unchanged(acc):
    acc.cache_put("summary:OPS-M04", "fp1", "보고서", {"mode": "meeting"})
    assert acc.cache_get("summary:OPS-M04", "fp1")["final"] == "보고서"
    assert acc.cache_get("summary:OPS-M04", "fp2") is None      # 원장이 바뀌면 새로 만든다


def test_cache_key_same_target_and_extra_request():
    import server
    k = server.cache_key
    assert k("kjh", "OPS-M04 회의 정리해줘") == k("pdy", "M04 회의 요약 부탁") == "summary:OPS-M04"
    assert k("kjh", "M04 정리해줘 할 일만") is None              # 추가 요청은 새로 처리
    assert k("kjh", "M04 다시 정리해줘") is None
    assert k("kjh", "SSE는 왜 골랐어?") != k("yhr", "SSE는 왜 골랐어?")   # 질문은 권한이 달라 사용자별
    assert k("kjh", "캐시는 어떻게 했어?") != k("kjh", "캐시는 어떻게 했어?", "CSB")   # 범위가 다르면 다른 요청
    assert k("kjh", "정리해줘", "", "OPS-M04") == "summary:OPS-M04"           # 회의를 고르고 '정리해줘'만 써도 같은 요청


def test_once_grant_is_used_up_on_actual_open(acc):
    rid = acc.request_access("yhr", "OPS-M04", "")
    acc.decide_request(rid, "kjh", True, "user", "once")
    assert acc.can_view_meeting("yhr", M04)                  # 목록 표시는 열람권을 쓰지 않음
    assert acc.can_view_meeting("yhr", M04, consume=True)    # 실제로 여는 순간 사용
    assert not acc.can_view_meeting("yhr", M04)


def test_scope_all_and_project_duration(acc):
    rid = acc.request_access("yhr", "OPS-M04", "")
    acc.decide_request(rid, "kjh", True, "all", "project")   # OPS는 진행 중 → 유효
    assert acc.can_view_meeting("jmh", {**M04, "attendees": []})
    rid = acc.request_access("yhr", "CSB-M09", "")
    acc.decide_request(rid, "kjh", True, "user", "project")  # CSB는 종료된 프로젝트 → 무효
    assert not acc.can_view_meeting("yhr", {"meeting_id": "CSB-M09", "attendees": []})
    rid = acc.request_access("yhr", "CSB-M09", "")
    acc.decide_request(rid, "kjh", True, "user", "forever")
    assert acc.can_view_meeting("yhr", {"meeting_id": "CSB-M09", "attendees": []})


def test_until_date_duration(acc):
    rid = acc.request_access("yhr", "OPS-M04", "")
    acc.decide_request(rid, "kjh", True, "user", "until", "2099-12-31")
    assert acc.can_view_meeting("yhr", M04)
    assert "2099-12-31까지" in acc.notifications("yhr")[0]["text"]
    rid = acc.request_access("lsj", "OPS-M05", "")
    acc.decide_request(rid, "kjh", True, "user", "until", "2020-01-01")   # 지난 날짜 → 무효
    assert not acc.can_view_meeting("lsj", {"meeting_id": "OPS-M05", "attendees": []})
