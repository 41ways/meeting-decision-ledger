"""드리프트 보고서 — 관리자 콘솔 '품질 감시' 탭과 같은 판정을 터미널에서 본다 (docs/pipeline_ops.md §3).

사용: .venv/bin/python scripts/drift_report.py [--version pv-…] [--window 30] [--json]
     경보가 있으면 종료 코드 1 (정기 실행·알림에 붙이기 쉽게)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger import monitor  # noqa: E402
from ledger.metrics import MetricsStore  # noqa: E402
from ledger.pipeline_registry import Registry, current  # noqa: E402
from ledger.records import MeetingRecords  # noqa: E402
from ledger.store import LedgerStore  # noqa: E402

MARK = {"정상": "  ", "주의": "△ ", "경보": "▲ ", "부족": "· "}


def to_text(rep: dict) -> str:
    """
    보고서를 사람이 읽는 표로
    Args:
        rep: monitor.report 결과
    Returns:
        Markdown 문자열
    """
    w = rep["window"]
    lines = [f"# 품질 감시 — 실행 {rep['running']['id']} ({rep['running']['status'] or '미등록'})",
             f"champion: {(rep['champion'] or {}).get('id') or '없음'}",
             f"기준선 {w['base']['version']} {w['base']['count']}건 ({w['base']['from']} ~ {w['base']['to']}) · "
             f"최근 {w['recent']['version']} {w['recent']['count']}건 ({w['recent']['from']} ~ {w['recent']['to']})",
             f"경보 {rep['alerts']} · 주의 {rep['warnings']}", "",
             "| | 묶음 | 지표 | 기준선 | 최근 | 검정 | 판정 |", "|---|---|---|---|---|---|---|"]
    for f in rep["findings"]:
        lines.append(f"| {MARK[f['level']]} | {f['group']} | {f['metric']} | {f['base']} | {f['recent']} | "
                     f"{f['test']} | {f['level']} |")
    nodes = rep.get("nodes", {}).get("recent") or []
    if nodes:
        lines += ["", "노드별 지연 (최근 창, 중앙값·최대 ms): "
                  + ", ".join(f"{x['node']} {x['median_ms']}·{x['max_ms']}" for x in nodes)]
    pol = rep.get("policy")
    if pol:
        lines += ["", f"정책: 현행 {pol['current']} · 지난번 확인 {pol['seen']}"
                  + (" — 개정 감지, 재검토 필요 결정:" if pol["changed"] else "")]
        for d in pol["review"]:
            lines.append(f"- {d['decision_id']} {d['statement']} ("
                         + ", ".join(f"{r['rule_id']} {r['change']}·{r['how']}" for r in d["rules"]) + ")")
    lines += ["", "버전별 사람 신호 (승인·반려·수정·닫기, 반려율·수정률)"]
    for v, h in rep["human"].items():
        lines.append(f"- {v}: {h['approved']}·{h['rejected']}·{h['edited']}·{h['closed']}, "
                     f"{h['reject_rate']}·{h['edit_rate']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """
    명령행 진입점
    Args:
        argv: 인자 목록 (테스트용)
    Returns:
        종료 코드 (경보 있으면 1)
    """
    ap = argparse.ArgumentParser(description="드리프트 보고서")
    ap.add_argument("--version", help="최근 창으로 볼 버전 (기본: 지금 코드로 계산한 버전)")
    ap.add_argument("--window", type=int, default=monitor.RULES["window"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fast", default="ollama:qwen3:8b")
    ap.add_argument("--quality", default="ollama:qwen3:8b")
    a = ap.parse_args(argv)
    running = a.version or current(a.fast, a.quality)[0]
    rep = monitor.report(MetricsStore(), MeetingRecords(), Registry(), LedgerStore(), running=running,
                         rules={"window": a.window})
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else to_text(rep))
    return 1 if rep["alerts"] else 0


if __name__ == "__main__":
    sys.exit(main())
