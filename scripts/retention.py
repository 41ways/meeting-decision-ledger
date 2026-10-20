"""보존 작업 — 보존 기한(기본 90일)이 지난 회의의 원문과 논의 내용을 파기한다 (docs/설계_재구성안.md §3.5).

사용: .venv/bin/python scripts/retention.py [--days 90] [--today 2026-10-20] [--apply]
기본은 무엇을 지울지 보여 주기만 한다(시험 실행). --apply를 줘야 실제로 지운다.

지우는 것: 원문 파일, 발언 색인, 회의 요약, 미결의 내용·인용 (원문 요약이라 원문과 함께)
남기는 것: 회의 정보(날짜·참석자), 원장 결정과 근거 인용, 할 일, 미결 최소 항목(주제·회의·담당)
정책 근거: OPS-M05에서 정한 '회의 원문은 90일 후 파기, 결정 근거 인용만 원장에 보존'
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger.records import MeetingRecords  # noqa: E402
from ledger.store import DATA  # noqa: E402


def expired(records: MeetingRecords, today: str, days: int) -> list[dict]:
    """
    보존 기한이 지났고 아직 파기하지 않은 회의
    Args:
        records: 회의 기록
        today:   기준일 (YYYY-MM-DD)
        days:    보존 일수
    Returns:
        회의 목록
    """
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    return [m for m in records.meetings() if m.get("date") and m["date"] < cutoff and not m.get("transcript_deleted")]


def run(records: MeetingRecords, data_dir: Path, today: str, days: int, apply: bool, rag=None) -> list[str]:
    """
    보존 작업 실행 (apply=False면 계획만)
    Args:
        records:  회의 기록
        data_dir: 시드 루트 (r1_meetings 하위에 원문 파일)
        today:    기준일
        days:     보존 일수
        apply:    True면 실제로 지운다
        rag:      발언 색인 (None이면 색인은 건드리지 않음)
    Returns:
        한 줄씩의 실행(또는 계획) 기록
    """
    log = []
    for m in expired(records, today, days):
        mid = m["meeting_id"]
        f = data_dir / "r1_meetings" / f"{mid}.json"
        log.append(f"{mid} ({m['date']}): 원문 파일 {'삭제' if f.exists() else '없음'} · 발언 색인 삭제 · 요약·미결 내용 삭제"
                   + ("" if apply else "  [시험 실행 — 지우지 않음]"))
        if not apply:
            continue
        if f.exists():
            f.unlink()
        if rag is not None:
            rag.delete_transcript(mid)
        records.forget_meeting_details(mid, actor="retention")
    if not log:
        log.append(f"보존 기한({days}일)이 지난 회의 없음 (기준일 {today})")
    return log


def main() -> None:
    """명령행 인자를 받아 보존 작업을 실행한다."""
    from agent.ledger_graph import TODAY  # pylint: disable=import-outside-toplevel  # 그래프의 기준일과 같게
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--today", default=TODAY)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    rag = None
    if a.apply:
        from ledger.rag import LedgerRAG  # pylint: disable=import-outside-toplevel  # 시험 실행엔 임베딩 서버가 필요 없다
        rag = LedgerRAG()
    for line in run(MeetingRecords(), DATA, a.today, a.days, a.apply, rag):
        print(line)


if __name__ == "__main__":
    main()
