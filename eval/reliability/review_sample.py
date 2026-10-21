"""사람 검수용 표본 — 개발·시험 셋 각각 20%를 고정 시드로 뽑아 docs/reliability_review.md에 체크리스트와 함께 쓴다.

층화: 회의는 프로젝트마다 1건, 질문은 기대 상태별, 결정은 충돌 유무별로 비율을 맞춘다.
검사기 경고와 작성자가 남긴 판단 메모(notes)는 표본과 따로 '검수 필요' 목록에 모두 넣는다.

사용: .venv/bin/python eval/reliability/review_sample.py
"""
from __future__ import annotations

import json
import random
from collections import defaultdict

import dataset as D

OUT = D.ROOT / "docs" / "reliability_review.md"
SEED = 20261021
NOTES = D.REL / "review_notes.json"   # 작성자가 짚은 판단 지점 {정답 ID: 이유}


def strata_sample(items: list[dict], key, frac: float, rng: random.Random) -> list[dict]:
    """
    층마다 같은 비율로 뽑는다 (층이 작아도 최소 1건)
    Args:
        items: 뽑을 대상
        key:   항목 → 층 이름
        frac:  비율
        rng:   난수기 (고정 시드)
    Returns:
        뽑힌 항목 (원래 순서)
    """
    groups = defaultdict(list)
    for x in items:
        groups[key(x)].append(x)
    target = round(len(items) * frac)
    picked = []
    for _, xs in sorted(groups.items()):
        picked += rng.sample(xs, max(1, round(len(xs) * frac)))
    while len(picked) > target:                 # 반올림으로 넘친 만큼 큰 층에서 덜어 낸다
        big = max(groups, key=lambda g: sum(key(p) == g for p in picked))
        picked.remove(next(p for p in picked if key(p) == big))
    order = {id(x): n for n, x in enumerate(items)}
    return sorted(picked, key=lambda x: order[id(x)])


def main() -> None:
    """
    분할별 20% 표본과 체크리스트, 검수 필요 목록을 Markdown으로 쓴다
    """
    gold = D.load_gold()
    idm = D.id_map(gold)
    _, warn, _ = D.check(gold)
    notes = json.loads(NOTES.read_text(encoding="utf-8")) if NOTES.exists() else {}
    rows = ["# 신뢰도 데이터셋 사람 검수", "",
            "데이터셋(eval/reliability)의 정답이 맞는지 사람이 확인하는 목록. 개발·시험 셋 각각 20%를 고정 시드로 층화 추출했다.",
            "표본에서 오류가 나오면 같은 종류를 전체에서 찾아 고치고 `dataset.py --check`를 다시 돌린다.",
            "시험 셋 표본은 정답만 보고, 시험 셋 실행 결과를 보면서 고치지 않는다.", "",
            "## 체크리스트", "",
            "회의 (전사문을 처음부터 읽고)",
            "- [ ] 정답 결정이 모두 '이 회의에서 정한 것'인가 (회상·제안·보류·회의 밖 결정이 섞이지 않았나)",
            "- [ ] 빠진 결정이 없는가 (돌려 말한 결정, 진행자 단정 포함)",
            "- [ ] 근거 발언 번호(제안·확정)와 결정자가 맞는가",
            "- [ ] 주제 키가 맞는가 (같은 주제로 승인되면 앞 결정이 대체된다)",
            "- [ ] 기대 충돌(종류·근거 ID)이 회의 날짜 기준 원장·현행 정책·현행 문서와 맞는가, 예외의 프로젝트·기간·범위가 맞는가",
            "- [ ] 할 일 담당·기한이 원문 그대로인가 (정정 반영, 상대 날짜 환산, 요청자와 담당자 구분, 원문에 없으면 null)",
            "- [ ] 미결이 실제로 결론 없이 끝났는가, 함정 발언 분류가 맞는가",
            "- [ ] 문제 행동(권한·규정 위반)을 가상 인물만 하는가",
            "- [ ] 대화가 자연스러운가 (규칙에 맞춘 티가 나지 않는가)", "",
            "의견·질문",
            "- [ ] 시점(at) 원장 기준으로 기대 판정·상태가 맞는가",
            "- [ ] 근거 ID·핵심어가 정답 판단에 충분하고 지나치게 빡빡하지 않은가",
            "- [ ] '찾지 못함' 함정이 정말 어디에도 없는 사안인가 (다른 프로젝트에만 있는 것 포함)",
            "- [ ] 이어 묻기는 앞 질문 답을 가리키는가", ""]
    for split in ("dev", "test"):
        rng = random.Random(f"{SEED}-{split}")
        ms = {k: v for k, v in gold["meetings"].items() if v["split"] == split}
        by_prj = defaultdict(list)
        for mid in sorted(ms):
            by_prj[ms[mid]["project"]].append(mid)
        meetings = sorted(rng.choice(v) for v in by_prj.values())
        decs = [d for m in ms.values() for d in m["decisions"]]
        acts = [a for m in ms.values() for a in m["action_items"]]
        ops = [o for o in gold["opinions"] if o["split"] == split]
        qs = [q for q in gold["questions"] if q["split"] == split]
        sd = strata_sample(decs, lambda d: bool({c["type"] for c in d.get("conflicts", [])} & D.BLOCKING), 0.2, rng)
        sa = strata_sample(acts, lambda a: a["id"][:3], 0.2, rng)
        so = strata_sample(ops, lambda o: o["expected"], 0.2, rng)
        sq = strata_sample(qs, lambda q: q["expected_state"], 0.2, rng)
        name = "개발" if split == "dev" else "시험"
        rows += [f"## {name} 셋 표본", "",
                 f"회의 {len(meetings)}/{len(ms)} · 결정 {len(sd)}/{len(decs)} · 할 일 {len(sa)}/{len(acts)} · "
                 f"의견 {len(so)}/{len(ops)} · 질문 {len(sq)}/{len(qs)}", "",
                 "회의 전체 읽기: " + ", ".join(f"`{m}`" for m in meetings), "",
                 "| 확인 | 결정 | 원장 ID | 확정 발언 | 주제 | 기대 라벨 | 기대 충돌 | 내용 |", "|---|---|---|---|---|---|---|---|"]
        rows += [f"| [ ] | {d['id']} | {idm.get(d['id'], '')} | {d['seq']} | {d['topic']} | {d['expected_label']} | "
                 + ", ".join(f"{c['type']}:{'/'.join(c['against'])}" for c in d.get("conflicts", [])) + f" | {d['statement']} |"
                 for d in sd]
        rows += ["", "| 확인 | 할 일 | 배정 발언 | 담당 | 기한 | 내용 |", "|---|---|---|---|---|---|"]
        rows += [f"| [ ] | {a['id']} | {a['seq']} | {a.get('owner') or '-'} | {a.get('due') or '-'} | {a['statement']} |" for a in sa]
        rows += ["", "| 확인 | 의견 | 시점 | 기대 판정 | 근거 | 문장 |", "|---|---|---|---|---|---|"]
        rows += [f"| [ ] | {o['id']} | {o['at']} | {o['expected']} | {', '.join(o.get('against', []))} | {o['text']} |" for o in so]
        rows += ["", "| 확인 | 질문 | 시점 | 기대 상태 | 정답 ID | 문장 |", "|---|---|---|---|---|---|"]
        rows += [f"| [ ] | {q['id']} | {q['at']} | {q['expected_state']} | "
                 f"{', '.join(idm.get(x.strip('{}'), x) for x in q.get('expected_ids', []))} | {q['text']} |" for q in sq]
        rows.append("")
    rows += ["## 검수 필요 (표본과 별개로 모두 확인)", "",
             "작성자가 판단이 갈린다고 남긴 항목과 검사기 경고.", ""]
    rows += [f"- [ ] `{k}` — {v}" for k, v in notes.items()]
    rows += [f"- [ ] 검사기 경고: {w}" for w in warn]
    OUT.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"{OUT} — 메모 {len(notes)}건, 경고 {len(warn)}건")


if __name__ == "__main__":
    main()
