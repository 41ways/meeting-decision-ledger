"""검색 품질 평가 — 주제 후보와 회의 원문 검색의 점수 분포를 재서 임계값을 정한다.

실행: .venv/bin/python eval/retrieval_eval.py   (Ollama 임베딩 모델 필요, LLM은 쓰지 않음)
결과: eval/retrieval_results.md

- 주제: 기대 주제가 1위인가(top1), 3위 안인가(top3). 함정(목록에 없는 주제)은 1위 점수가 얼마나 높게 나오나
- 원문: 관련 발언이 3위 안에 있나(hit@3). 함정(논의한 적 없는 주제)의 1위 점수
두 분포(정답 vs 함정)가 갈리는 지점이 임계값 후보다.
"""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger.resolve import Resolver  # noqa: E402  pylint: disable=wrong-import-position
from ledger.thresholds import MIN_DISCUSSION_SCORE, MIN_TOPIC_SCORE  # noqa: E402  pylint: disable=wrong-import-position

CASES = json.loads((ROOT / "eval" / "retrieval_cases.json").read_text(encoding="utf-8"))
OUT = ROOT / "eval" / "retrieval_results.md"


def _stats(xs: list[float]) -> str:
    """
    점수 목록의 최소·중앙·최대 요약
    Args:
        xs: 점수 목록
    Returns:
        "최소 / 중앙 / 최대" 문자열 (비었으면 "—")
    """
    if not xs:
        return "—"
    return f"{min(xs):.3f} / {statistics.median(xs):.3f} / {max(xs):.3f}"


def topic_eval(rv: Resolver) -> tuple[list[str], dict]:
    """
    주제 후보 평가
    Args:
        rv: 해석기
    Returns:
        (보고서 줄 목록, 요약 수치 dict)
    """
    rows, ok1, ok3, real, trap_top, real_top, margins = [], 0, 0, 0, [], [], []
    for c in CASES["topic"]:
        cands = rv.topic_candidates(c["text"], k=3)
        top = cands[0] if cands else {"topic": "", "score": 0.0}
        gap = top["score"] - (cands[1]["score"] if len(cands) > 1 else 0.0)
        got = [x["topic"] for x in cands]
        if c["topic"]:
            real += 1
            ok1 += got[:1] == [c["topic"]]
            ok3 += c["topic"] in got
            (real_top if got[:1] == [c["topic"]] else []).append(top["score"])
            margins.append(gap)
            mark = "✓" if got[:1] == [c["topic"]] else ("△" if c["topic"] in got else "✗")
        else:
            trap_top.append(top["score"])
            mark = "함정"
        rows.append(f"| {mark} | {c['text'][:34]} | {c['topic'] or '(없음)'} | "
                    + ", ".join(f"{x['topic']} {x['score']:.3f}" for x in cands) + f" | {gap:.3f} |")
    summary = {"top1": ok1 / real, "top3": ok3 / real, "real_top": real_top, "trap_top": trap_top, "margins": margins,
               # 하한 적용 후: 함정에 주제를 붙인 수, 정답 주제를 하한 때문에 버린 수
               "trap_assigned": sum(s >= MIN_TOPIC_SCORE for s in trap_top),
               "real_dropped": sum(s < MIN_TOPIC_SCORE for s in real_top)}
    return rows, summary


def transcript_eval(rv: Resolver) -> tuple[list[str], dict]:
    """
    회의 원문 검색 평가 (기본 프로젝트 한정)
    Args:
        rv: 해석기
    Returns:
        (보고서 줄 목록, 요약 수치 dict)
    """
    rows, hit, real, rel_scores, irrel_scores, trap_top = [], 0, 0, [], [], []
    for c in CASES["transcript"]:
        res = rv.rag.search_transcript(c["text"], project_id=rv.project_id, k=3)
        keys = [f"{r['meeting_id']}:{r['seq']}" for r in res]
        if c["hits"]:
            real += 1
            ok = any(k in c["hits"] for k in keys)
            hit += ok
            for r, k in zip(res, keys):
                (rel_scores if k in c["hits"] else irrel_scores).append(r["_score"])
            mark = "✓" if ok else "✗"
        else:
            trap_top.append(res[0]["_score"] if res else 0.0)
            mark = "함정"
        rows.append(f"| {mark} | {c['text'][:30]} | "
                    + ", ".join(f"{k} {r['_score']:.3f}" for k, r in zip(keys, res)) + " |")
    return rows, {"hit3": hit / real, "rel": rel_scores, "irrel": irrel_scores, "trap_top": trap_top,
                  "trap_with_discussion": sum(s >= MIN_DISCUSSION_SCORE for s in trap_top),
                  "trap_total": len(trap_top),
                  "rel_dropped": sum(s < MIN_DISCUSSION_SCORE for s in rel_scores)}


def main() -> None:
    """평가를 돌리고 eval/retrieval_results.md에 쓴다."""
    rv = Resolver()
    t_rows, t = topic_eval(rv)
    r_rows, r = transcript_eval(rv)
    out = ["# 검색 품질 평가", "", "점수 = cosine 유사도. 분포는 최소 / 중앙 / 최대.", "",
           "## 요약", "",
           "| 항목 | 값 |", "|---|---|",
           f"| 주제 top1 정확도 | {t['top1']:.0%} |",
           f"| 주제 top3 포함률 | {t['top3']:.0%} |",
           f"| 주제 1위 점수 — 정답일 때 | {_stats(t['real_top'])} |",
           f"| 주제 1위 점수 — 함정(새 주제) | {_stats(t['trap_top'])} |",
           f"| 주제 1·2위 점수 차 (정답 있는 문장) | {_stats(t['margins'])} |",
           f"| 원문 hit@3 | {r['hit3']:.0%} |",
           f"| 원문 점수 — 관련 발언 | {_stats(r['rel'])} |",
           f"| 원문 점수 — 상위 3위 안 무관 발언 | {_stats(r['irrel'])} |",
           f"| 원문 1위 점수 — 함정(논의 없음) | {_stats(r['trap_top'])} |",
           "", f"## 하한 적용 후 (주제 {MIN_TOPIC_SCORE}, 원문 {MIN_DISCUSSION_SCORE})", "",
           "| 항목 | 값 |", "|---|---|",
           f"| 새 주제(함정)에 기존 주제를 붙인 수 | {t['trap_assigned']} / {len(t['trap_top'])} |",
           f"| 맞힌 주제를 하한 때문에 버린 수 | {t['real_dropped']} / {len(t['real_top'])} |",
           f"| 논의 없는 질문에 '관련 논의'를 붙인 수 | {r['trap_with_discussion']} / {r['trap_total']} |",
           f"| 관련 발언을 하한 때문에 버린 수 | {r['rel_dropped']} / {len(r['rel'])} |",
           "", "## 주제 후보", "", "| | 문장 | 기대 | 후보 (점수) | 1·2위 차 |", "|---|---|---|---|---|",
           *t_rows, "", "## 회의 원문", "", "| | 질의 | 상위 3 (점수) |", "|---|---|---|", *r_rows]
    OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out[:24]))


if __name__ == "__main__":
    main()
