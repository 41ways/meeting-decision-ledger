"""교차검증 효과 — '수정 후'(교차검증 끔)와 '수정 후 + 교차검증'을 같은 항목만 놓고 비교한다.

교차검증 회차가 중간까지만 돌았어도 공정하게 비교하려고, 두 회차 모두 교차검증 회차에 있는 회의·의견·질문만 남겨 채점한다.
사용: .venv/bin/python eval/reliability/crosscheck_compare.py [끈 회차 이름표] [켠 회차 이름표]
결과: eval/reliability/results_dev_crosscheck_compare.md
"""
from __future__ import annotations

import json
import sys

import dataset as D
from compare import median_ci
from run import PAUSE_SEC, fmt, summarize

ROWS = [("자동 처리율 · 전체", "자동 처리율"), ("자동 처리 정확도 · 전체", "자동 처리 정확도"),
        ("사람에게 넘긴 비율 · 전체", "사람에게 넘긴 비율"), ("넘긴 것 중 애매·오류 · 전체", "└ 그중 실제로 애매·오류"),
        ("결정 정밀도", "결정 정밀도"), ("결정 재현율", "결정 재현율"), ("충돌 오탐(충돌 없는 결정 중)", "충돌 오탐"),
        ("충돌·변경 탐지율", "충돌·변경 탐지율"), ("질문 정답률", "질문 정답률"), ("의견 판정 정확", "의견 판정 정확")]
KIND_KO = {"meetings": "회의 정리", "opinions": "의견 검토", "questions": "근거 질문"}


def main(off: str, on: str) -> None:
    """
    두 회차를 같은 항목으로 맞춰 표로 쓴다
    Args:
        off: 교차검증 끈 회차 이름표 (results_dev_<off>.json)
        on:  교차검증 켠 회차 이름표 (.cache/raw_dev_<on>.json — 진행 중이어도 읽는다)
    """
    gold = D.load_gold()
    raw_off = json.loads((D.REL / f"results_dev_{off}.json").read_text(encoding="utf-8"))["raw"]
    raw_on = json.loads((D.REL / ".cache" / f"raw_dev_{on}.json").read_text(encoding="utf-8"))
    keep = {k: set(raw_on.get(k, {})) for k in ("meetings", "opinions", "questions")}
    sub_off = {k: {i: v for i, v in raw_off[k].items() if i in keep[k]} for k in keep} | {"id_map": raw_off["id_map"]}
    a, b = summarize(gold, sub_off), summarize(gold, raw_on)
    n = {k: len(keep[k]) for k in keep}
    full = all(n[k] == len(raw_off[k]) for k in keep)
    rows = ["# 교차검증 효과 — 개발 셋", "",
            f"- 비교 범위: {'개발 셋 전체' if full else '교차검증 회차가 끝낸 항목만 (부분 결과)'} — 회의 {n['meetings']}/20 · 의견 {n['opinions']}/30 · 질문 {n['questions']}/100",
            "- 끈 회차: results_dev_" + off + " (main 6dba64c), 켠 회차: main dd11647(7f571eb 포함) + CROSSCHECK=1. 코드 차이는 근거 없는 '승인 대기' 차단과 추출 시간 제한 완화.",
            "", f"| 지표 | 교차검증 끔 | 교차검증 켬 |", "|---|---|---|"]
    rows += [f"| {name} | {fmt(a['metrics'][k]) if k in a['metrics'] else '-'} | {fmt(b['metrics'][k]) if k in b['metrics'] else '-'} |"
             for k, name in ROWS]
    for k in ("meetings", "opinions", "questions"):
        rows.append(f"| 응답 시간 중앙값 · {KIND_KO[k]} | "
                    + " | ".join(median_ci([x['sec'] for x in r.get(k, {}).values() if x['sec'] <= PAUSE_SEC])
                                 for r in (sub_off, raw_on)) + " |")
    xc = b.get("crosscheck") or {}
    if xc:
        rows += ["", "## 교차검증 이의", "", "| 검증 대상 | 검증 건수 | 이의(재심·확인 필요) 건수 | 이의 중 8b가 실제로 틀린 비율 | 확인 필요로 넘긴 것 중 8b 오류 | 4/4 동의일 때 8b 정확 |",
                 "|---|---|---|---|---|---|"]
        rows += [f"| {k} | {v['검증 건수']['n']} | {v['이의 있음 중 8b 오류']['n']} | {fmt(v['이의 있음 중 8b 오류'])} | "
                 f"{fmt(v['이견으로 넘긴 것 중 8b 오류'])} | {fmt(v['4/4 동의일 때 8b 정확'])} |" for k, v in xc.items()]
        rows += ["", "8b가 틀렸는지는 정답으로 판단: decision = 결정이라고 한 발언이 정답 결정이 아님, relation = 같다·무관하다고 한 기준이 정답 충돌 근거에 있음, pick = 고른 결정이 질문 정답이 아님."]
    rows += ["", "95% 구간: †는 회의 단위 부트스트랩, 나머지 비율은 Wilson, 응답 시간 중앙값은 요청 단위 부트스트랩."]
    (D.REL / "results_dev_crosscheck_compare.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))


if __name__ == "__main__":
    main(*(sys.argv[1:3] if len(sys.argv) > 2 else ("fixed", "crosscheck")))
