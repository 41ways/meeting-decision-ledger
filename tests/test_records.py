"""회의 기록(회의 목록·항목·사안·이력) 단위 테스트."""
# pylint: disable=missing-function-docstring,redefined-outer-name  # pytest fixture 관용
import pytest

from ledger.records import MeetingRecords


@pytest.fixture
def rec(tmp_path):
    r = MeetingRecords(tmp_path / "ledger.db")
    r.sync_meetings()
    return r


def test_meeting_info_comes_from_transcript_header_and_survives_deletion(rec, tmp_path):
    m = rec.meeting("OPS-M04")
    assert m["date"] == "2026-10-13" and [a["name"] for a in m["attendees"]][:2] == ["서지후", "장상민"]
    assert m["attendees"][0]["role"] == "PL"          # 발언에 붙은 역할
    rec.forget_meeting_details("OPS-M04")
    assert rec.meeting("OPS-M04")["date"] == "2026-10-13"      # 원문 내용을 지워도 회의 정보는 남는다


OPEN_M03 = {"kind": "open", "seq": 10, "statement": "캐시 대상", "topic": "cache", "label": "미결", "state": "open"}
DEC_M04 = {"kind": "decision", "seq": 11, "statement": "캐시는 런북 문서에만", "topic": "cache", "label": "승인 대기",
           "state": "held"}


def test_resaving_does_not_duplicate_and_open_item_links_to_later_decision(rec):
    rec.save_meeting("OPS-M03", None, [OPEN_M03], "v")
    rec.save_meeting("OPS-M03", None, [OPEN_M03], "v")             # 다시 정리해도
    assert len(rec.items(meeting_id="OPS-M03")) == 1                # 두 번 생기지 않는다
    out = rec.save_meeting("OPS-M04", None, [DEC_M04], "v")
    assert out["linked"] == [{"open": "OPS-M03:open:10", "to": "OPS-M04:decision:11"}]
    assert rec.item("OPS-M03:open:10")["state"] == "linked" and not rec.open_items()
    rec.link_decision("OPS-M04:decision:11", "OPS-0011")
    assert rec.item("OPS-M03:open:10")["linked_to"] == "OPS-0011"   # 원장 ID를 따라간다
    assert [i["item_id"] for i in rec.thread_of_topic("PRJ-OPSAI", "cache")] == ["OPS-M03:open:10", "OPS-M04:decision:11"]
    assert {e["kind"] for e in rec.events()} >= {"meeting_processed", "item_linked", "decision_proposed"}


def test_open_item_without_topic_is_not_linked(rec):
    rec.save_meeting("OPS-M03", None, [{**OPEN_M03, "topic": ""}], "v")
    assert rec.save_meeting("OPS-M04", None, [DEC_M04], "v")["linked"] == []   # 주제를 모르면 억지로 잇지 않는다


def test_human_close_and_reprocess_keeps_closed_state(rec):
    rec.save_meeting("OPS-M03", None, [OPEN_M03], "v")
    rec.close_open("OPS-M03:open:10", "정한결", "회의 밖에서 하지 않기로 함")
    rec.save_meeting("OPS-M03", None, [OPEN_M03], "v2")
    assert rec.item("OPS-M03:open:10")["state"] == "closed"
    with pytest.raises(ValueError):
        rec.close_open("OPS-M03:open:10", "정한결", "")


def test_forget_details_trims_open_items_but_keeps_decisions(rec):
    rec.save_meeting("OPS-M03", {"topic": "점검", "lines": []}, [OPEN_M03, {**DEC_M04, "seq": 3, "topic": "x"}], "v")
    out = rec.forget_meeting_details("OPS-M03")
    assert out == {"summary_cleared": True, "open_trimmed": 1}
    assert rec.item("OPS-M03:open:10")["statement"].startswith("(원문 파기")
    assert rec.item("OPS-M03:decision:3")["statement"] == "캐시는 런북 문서에만"


def test_retention_dry_run_then_apply(tmp_path):
    import json
    import shutil
    from scripts import retention                     # noqa: PLC0415
    data = tmp_path / "data"
    (data / "r1_meetings").mkdir(parents=True)
    shutil.copy("data/project/users.json", data / "users.json")
    for mid, day in (("OPS-M01", "2026-06-01"), ("OPS-M02", "2026-10-01")):
        (data / "r1_meetings" / f"{mid}.json").write_text(json.dumps(
            {"meeting_id": mid, "project_id": "PRJ-OPSAI", "date": day, "title": "t", "attendees": [], "segments": []}))
    rec = MeetingRecords(tmp_path / "ledger.db")
    rec.sync_meetings(data)
    rec.save_meeting("OPS-M01", {"topic": "x", "lines": []}, [OPEN_M03], "v")
    plan = retention.run(rec, data, "2026-10-20", 90, apply=False)
    assert len(plan) == 1 and "OPS-M01" in plan[0] and "시험 실행" in plan[0]
    assert (data / "r1_meetings" / "OPS-M01.json").exists()                  # 시험 실행은 지우지 않는다
    retention.run(rec, data, "2026-10-20", 90, apply=True)
    assert not (data / "r1_meetings" / "OPS-M01.json").exists() and (data / "r1_meetings" / "OPS-M02.json").exists()
    m = rec.meeting("OPS-M01")
    assert m["transcript_deleted"] == 1 and m["summary"] is None and m["date"] == "2026-06-01"   # 회의 정보는 남는다
    assert rec.item("OPS-M01:open:10")["statement"].startswith("(원문 파기")
    assert "지난 회의 없음" in retention.run(rec, data, "2026-10-20", 90, apply=True)[0]
