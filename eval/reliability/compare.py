"""여러 실행 결과를 한 표로 — 개발 셋 회차들과 시험 셋을 나란히, 발표용 핵심 지표를 맨 위에.

사용: .venv/bin/python eval/reliability/compare.py <출력 이름> <분할[/이름표]:열 제목> ...
  예: compare.py compare dev/baseline:기준선 dev/index:"용어 색인" dev/fixed:"수정 후" test:시험
  → eval/reliability/results_compare.md  (results_<분할>[_<이름표>].json을 읽는다)
"""
from __future__ import annotations

import json
import random
import sys

import dataset as D
from run import PAUSE_SEC, fmt

HEAD = [("자동 처리율 · 전체", "자동 처리율"), ("자동 처리 정확도 · 전체", "자동 처리 정확도"),
        ("사람에게 넘긴 비율 · 전체", "사람에게 넘긴 비율"), ("넘긴 것 중 애매·오류 · 전체", "└ 그중 실제로 애매·오류"),
        ("결정 재현율", "결정 재현율"), ("결정 정밀도", "결정 정밀도")]
KEYS = ["할 일 담당·기한 모두 정확", "미결 재현율", "충돌·변경 탐지율", "충돌 오탐(충돌 없는 결정 중)", "회의 처리 오류",
        "의견 판정 정확", "질문 정답률", "'찾지 못함' 함정 정답률",
        "자동 처리율 · 결정", "자동 처리 정확도 · 결정", "자동 처리율 · 질문", "자동 처리 정확도 · 질문"]
KIND_KO = {"meetings": "회의 정리", "opinions": "의견 검토", "questions": "근거 질문"}


def median_ci(secs: list[float], reps: int = 1000) -> str:
    """
    응답 시간 중앙값과 95% 부트스트랩 구간
    Args:
        secs: 요청별 응답 시간(초)
        reps: 반복 횟수
    Returns:
        '138.3초 (131.0–150.2)' 형식. 값이 없으면 '-'
    """
    if not secs:
        return "-"
    s = sorted(secs)
    med = lambda xs: sorted(xs)[(len(xs) - 1) // 2]  # noqa: E731
    rng = random.Random(0)
    boots = sorted(med([rng.choice(s) for _ in s]) for _ in range(reps))
    return f"{med(s):.1f}초 ({boots[int(0.025 * reps)]:.1f}–{boots[int(0.975 * reps) - 1]:.1f})"


def load(col: str) -> tuple[str, dict]:
    """
    '분할[/이름표]:제목' 하나를 읽는다
    Args:
        col: 열 지정
    Returns:
        (제목, 결과 JSON)
    """
    spec, _, title = col.partition(":")
    split, _, tag = spec.partition("/")
    f = D.REL / f"results_{split}{'_' + tag if tag else ''}.json"
    return title or spec, json.loads(f.read_text(encoding="utf-8"))


def main(out: str, cols_spec: list[str]) -> None:
    """
    결과 JSON 여러 개를 표로 쓴다 (핵심 지표 → 세부 지표 → 응답 시간)
    Args:
        out:       출력 파일 이름표 (results_<out>.md)
        cols_spec: 열 지정 목록
    """
    cols = [load(c) for c in cols_spec]
    rows = ["# 신뢰도 평가 결과 비교", "",
            "| 실행 | 분할 | 코드 | 시각 | 걸린 시간(분) |", "|---|---|---|---|---|"]
    rows += [f"| {t} | {r['meta'].get('split', '')} | {r['meta']['commit']} | {r['meta']['run_at']} | {r['meta']['wall_min']} |"
             for t, r in cols]
    hdr = "| 지표 | " + " | ".join(t for t, _ in cols) + " |"
    sep = "|---|" + "---|" * len(cols)
    rows += ["", "## 핵심 지표 (95% 신뢰구간 · 표본)", "", hdr, sep]
    rows += [f"| {name} | " + " | ".join(fmt(r["metrics"][k]) if k in r["metrics"] else "-" for _, r in cols) + " |"
             for k, name in HEAD]
    for k in ("meetings", "opinions", "questions"):
        rows.append(f"| 응답 시간 중앙값 · {KIND_KO[k]} | " + " | ".join(
            median_ci([x["sec"] for x in r["raw"].get(k, {}).values() if x["sec"] <= PAUSE_SEC]) for _, r in cols) + " |")
    rows += ["", "## 세부 지표", "", hdr, sep]
    rows += [f"| {k} | " + " | ".join(fmt(r["metrics"][k]) if k in r["metrics"] else "-" for _, r in cols) + " |"
             for k in KEYS]
    rows += ["", "## 응답 시간 상위 5% (초)", "", hdr, sep]
    rows += [f"| {KIND_KO[k]} | " + " | ".join(str(r["cost"][k]["p95_sec"]) if k in r["cost"] else "-" for _, r in cols) + " |"
             for k in ("meetings", "opinions", "questions")]
    rows += ["", "- 95% 구간: †는 회의 단위 부트스트랩 1,000회, 나머지 비율은 Wilson, 응답 시간 중앙값은 요청 단위 부트스트랩 1,000회.",
             "- 원장은 매 시점 정답 기준(팀장이 정답대로 검토했다고 가정) — 항목별 독립 채점. 정의는 각 results_*.md 끝.",
             "- 시험 셋은 한 번만 쟀다. 시험 셋 결과를 보고 코드를 고치지 않는다 (README 시험 셋 원칙)."]
    (D.REL / f"results_{out}.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
