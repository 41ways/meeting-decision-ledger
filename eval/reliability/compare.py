"""같은 분할을 여러 번 잰 결과를 한 표로 — 기준선 · 용어 색인 · 교차검증 비교.

사용: .venv/bin/python eval/reliability/compare.py dev baseline:기준선 index:"용어 색인" crosscheck:교차검증
결과: eval/reliability/results_<split>_compare.md
"""
from __future__ import annotations

import json
import sys

import dataset as D
from run import fmt

KEYS = ["자동 처리율 · 전체", "자동 처리 정확도 · 전체", "사람에게 넘긴 비율 · 전체", "넘긴 것 중 애매·오류 · 전체",
        "결정 재현율", "결정 정밀도", "할 일 담당·기한 모두 정확", "미결 재현율", "충돌·변경 탐지율",
        "충돌 오탐(충돌 없는 결정 중)", "회의 처리 오류", "의견 판정 정확", "질문 정답률", "'찾지 못함' 함정 정답률",
        "자동 처리율 · 결정", "자동 처리 정확도 · 결정", "자동 처리율 · 질문", "자동 처리 정확도 · 질문"]


def main(split: str, runs: list[str]) -> None:
    """
    결과 JSON 여러 개를 지표 × 실행 표로 쓴다
    Args:
        split: dev / test
        runs:  '이름표:열 제목' 목록 (results_<split>_<이름표>.json)
    """
    cols = []
    for r in runs:
        tag, _, title = r.partition(":")
        f = D.REL / f"results_{split}_{tag}.json"
        if f.exists():
            cols.append((title or tag, json.loads(f.read_text(encoding="utf-8"))))
    rows = [f"# 신뢰도 평가 비교 — {'개발' if split == 'dev' else '시험'} 셋", "",
            "| 실행 | 코드 | 시각 | 걸린 시간(분) |", "|---|---|---|---|"]
    rows += [f"| {t} | {r['meta']['commit']} | {r['meta']['run_at']} | {r['meta']['wall_min']} |" for t, r in cols]
    rows += ["", "| 지표 | " + " | ".join(t for t, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    for k in KEYS:
        rows.append(f"| {k} | " + " | ".join(fmt(r["metrics"][k]) if k in r["metrics"] else "-" for _, r in cols) + " |")
    rows += ["", "| 응답 시간 중앙값 / 상위 5% (초) | " + " | ".join(t for t, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    for k in ("meetings", "opinions", "questions"):
        rows.append(f"| {k} | " + " | ".join(f"{r['cost'][k]['median_sec']} / {r['cost'][k]['p95_sec']}"
                                             if k in r["cost"] else "-" for _, r in cols) + " |")
    rows += ["", "†: 회의 단위 부트스트랩 95% 구간, 나머지는 Wilson 95% 구간. 항목별 상세는 results_<split>_<이름표>.md."]
    (D.REL / f"results_{split}_compare.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
