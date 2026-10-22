"""사람이 고친 흔적에서 놓침을 찾는 보고서."""
# pylint: disable=missing-function-docstring
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import feedback  # noqa: E402


def ev(kind, target, detail, at="2026-10-22T10:00:00"):
    return {"kind": kind, "target": target, "detail": json.dumps(detail, ensure_ascii=False), "at": at, "actor": "정한결"}


def test_topic_fix_to_existing_topic_is_a_miss_with_alias_candidates_and_closes_when_alias_added():
    decs = {"OPS-0031": {"statement": "알림은 메신저 웹훅 채널로 보낸다"}}
    events = [ev("decision_edited", "OPS-0031", {"changed": {"topic": {"before": "", "after": "release"}}})]
    r = feedback.report(events, decs, {"release", "cache"})
    m = r["misses"][0]
    assert m["kind"] == "주제 놓침" and m["status"] == "열림" and m["alias_candidates"]
    events.append(ev("aliases_added", "release", {"topic": "release", "added": ["웹훅"]}))
    assert feedback.report(events, decs, {"release", "cache"})["misses"][0]["status"] == "반영됨"


def test_new_topic_and_wrong_topic_and_rejection_are_separated():
    decs = {"A": {"statement": "공지는 슬랙으로"}, "B": {"statement": "캐시는 런북만"}}
    events = [ev("topic_added", "notice.channel", {}),
              ev("decision_edited", "A", {"changed": {"topic": {"before": "", "after": "notice.channel"}}}),
              ev("decision_edited", "B", {"changed": {"topic": {"before": "session.store", "after": "cache"},
                                                      "statement": {"before": "캐시", "after": "캐시는 런북만"}}}),
              ev("decision_rejected", "B", {"reason": "결정 아님"})]
    r = feedback.report(events, decs, {"cache", "session.store"})
    kinds = sorted(m["kind"] for m in r["misses"])
    assert kinds == ["목록 밖 사안", "문장 수정", "주제 오분류"]
    assert r["summary"]["반려"] == 1 and r["rejections"][0]["reason"] == "결정 아님"
