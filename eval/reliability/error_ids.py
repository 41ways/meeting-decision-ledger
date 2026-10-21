"""오류 항목 ID 목록 — 실행 결과에서 틀린 항목을 원인별로 나눠 ID를 모두 적는다 (모델 호출 없음).

고칠 때 바로 열어 볼 수 있게 회의 발언 번호·질문 ID를 남기고, 고친 뒤 같은 분류로 다시 세어 비교한다.
원인 판별은 저장된 결과와 전사문만 보는 근사치다 (gate 규칙은 기준선 코드의 정규식을 그대로 옮겼다).

사용: .venv/bin/python eval/reliability/error_ids.py results_dev_baseline.json > 출력.md
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict

import dataset as D
import run as R

# 기준선 코드(agent/ledger_graph.py)의 결정·할 일 판별 규칙 — 결정을 놓친 원인을 가르는 데만 쓴다
CONFIRM = re.compile(r"걸로|겠습니다|확정|하죠|합시다|바꾸죠|가죠|그렇게 하|갑시다")
NOT_CONFIRM = re.compile(r"확정은 아닙|아직 확정|결론 내지|다음에 다시|보고 정할|검토해")
ASSIGN = re.compile(r"까지|주세요|부탁")
# 권한표 주제 연결표 — 파일로 빠진 버전(data/project/auth_topics.json)이면 데이터셋 세계 연결표까지 합친다
_AT = D.BASE / "auth_topics.json"
AUTH_TOPIC = ({*json.loads(_AT.read_text(encoding="utf-8"))["map"],
               *json.loads((D.WORLD / "auth_topics.json").read_text(encoding="utf-8"))["map"]} if _AT.exists() else
              {"schedule", "budget", "perf.latency", "scope.core", "answer.grounding", "llm.external", "meeting.retention"})


def segs_of(mid: str) -> dict:
    """
    회의 전사문 발언
    Args:
        mid: 회의 ID
    Returns:
        발언 번호 → 발언
    """
    return {s["seq"]: s for s in json.loads((D.MEETINGS / f"{mid}.json").read_text(encoding="utf-8"))["segments"]}


def meetings(g: dict, raw: dict, idm: dict) -> dict:
    """
    결정 놓침·틀린 결정·충돌 놓침을 원인별로
    Args:
        g:   정답
        raw: 회의 실행 결과
        idm: 정답 ID → 원장 ID
    Returns:
        {표 이름: {원인: [항목 표기]}}
    """
    out = {"결정 놓침": defaultdict(list), "틀린 결정": defaultdict(list), "충돌·변경 놓침": defaultdict(list)}
    ref = lambda a: idm.get(a.strip("{}"), a.strip("{}"))  # noqa: E731
    for mid, p in raw.items():
        gm, segs, preds = g["meetings"][mid], segs_of(mid), p.get("decisions", [])
        used, match = set(), {}
        for gd in gm["decisions"]:
            # 채점(run.score_meetings)과 같은 짝짓기 — 확정 발언 번호가 같은 예측을 먼저
            cands = sorted((i for i, x in enumerate(preds) if i not in used and x["seq"] in gd["evidence_seq"]),
                           key=lambda i: preds[i]["seq"] != gd["seq"])
            i = cands[0] if cands else None
            if i is not None:
                used.add(i)
                match[gd["id"]] = preds[i]
                continue
            s = segs[gd["seq"]]
            k = ("gate: 보류 표현" if NOT_CONFIRM.search(s["text"]) else
                 "gate: '까지·주세요·부탁'을 할 일로 봄" if ASSIGN.search(s["text"]) else
                 "gate: 확정 표현 밖 + 진행자 아님" if not (CONFIRM.search(s["text"]) or s["role"] in ("PM", "PL")) else
                 "추출 LLM이 결정으로 안 뽑음")
            out["결정 놓침"][k].append(f"{gd['id']} (#{gd['seq']} {s['role']})")
        gseq = {q for gd in gm["decisions"] for q in gd["evidence_seq"]}
        traps = {t["seq"]: t["kind"] for t in gm.get("traps", [])}
        aseq = {q for a in gm["action_items"] for q in a["evidence_seq"]}
        useq = {q for u in gm.get("undecided", []) for q in u["evidence_seq"]}
        for i, x in enumerate(preds):
            if i in used:
                continue
            q, role = x["seq"], segs.get(x["seq"], {}).get("role", "?")
            k = ("같은 결정 중복" if q in gseq else f"함정({traps[q]})" if q in traps else "할 일 배정·수락" if q in aseq
                 else "보류(미결) 발언" if q in useq else f"그 밖의 발언({'진행자' if role in ('PM', 'PL') else '참석자'})")
            out["틀린 결정"][k].append(f"{mid}#{q}")
        for gd in gm["decisions"]:
            d = match.get(gd["id"])
            for c in gd.get("conflicts", []):
                if c["type"] == "past_project":
                    continue
                got = set()
                if d:
                    got = {a for x in d["conflicts"] if x["type"] != "past_project" for a in x["against"]} \
                        | set(d["changes"]) | set(d["confirms"])
                if {ref(a) for a in c["against"]} & got:
                    continue
                k = ("결정을 못 뽑음" if not d else
                     "판정 보류(주제 불확실 등) → 사람에게" if d["status"] == "needs_approval" else
                     "권한: 주제가 권한표 연결표에 없음" if c["type"] == "authority" and d["topic"] not in AUTH_TOPIC
                     and c["against"][0] != "AUTH-01" else
                     "권한: 금액·기간 기준·참석자" if c["type"] == "authority" else
                     "주제가 달라 비교 대상 못 찾음" if d["topic"] != gd["topic"] else
                     "정책을 못 찾았거나 같다고 판정" if c["type"] == "policy" else "같은 주제인데 다름을 못 봄")
                out["충돌·변경 놓침"][k].append(f"{gd['id']} {c['type']}:{'/'.join(c['against'])}")
    return out


def questions(g: dict, raw: dict, idm: dict) -> dict:
    """
    질문·의견 오답을 원인별로
    Args:
        g:   정답
        raw: 실행 결과 전체
        idm: 정답 ID → 원장 ID
    Returns:
        {표 이름: {원인: [항목 표기]}}
    """
    out = {"질문 오답": defaultdict(list), "의견 오답": defaultdict(list)}
    qg = {q["id"]: q for q in g["questions"]}
    for qid, p in raw.get("questions", {}).items():
        q = qg[qid]
        if R.answer_ok(q, p, idm):
            continue
        short = (p.get("final") or "").split("\n\n### ")[0]
        key = q.get("scope_project") or qid[:3]
        other = [i for i in R.ANY_ID.findall(short) if re.fullmatch(r"[A-Z]{3}-\d{4}", i) and i[:3] != key]
        k = (f"경로 오분류({p.get('mode')})" if p.get("mode") not in ("question", "fact") else
             "사실 조회 되묻기" if (p.get("final") or "").startswith(R.CLARIFY) else
             "다른 프로젝트 결정으로 답함" if other and q.get("kind") != "cross_project" else
             "무관한 승인 대기를 씀" if "승인 대기" in short and q["expected_state"] != "승인 대기" else
             f"{q['expected_state']} → 라벨 {p.get('label')}")
        out["질문 오답"][k].append(f"{qid}({q['expected_state']})")
    og = {o["id"]: o for o in g["opinions"]}
    for oid, p in raw.get("opinions", {}).items():
        o = og[oid]
        if p.get("verdict") == o["expected"]:
            continue
        k = f"경로 오분류({p.get('mode')})" if p.get("mode") != "opinion" else f"{o['expected']} → {p.get('verdict')}"
        out["의견 오답"][k].append(oid)
    return out


def main(path: str) -> None:
    """
    결과 파일 하나의 오류 항목 ID 목록을 Markdown으로 출력
    Args:
        path: eval/reliability 아래 결과 JSON 파일 이름
    """
    g = D.load_gold()
    raw = json.loads((D.REL / path).read_text(encoding="utf-8"))["raw"]
    idm = raw.get("id_map", {})
    tabs = {**meetings(g, raw.get("meetings", {}), idm), **questions(g, raw, idm)}
    rows = [f"## 부록 — 오류 항목 ID ({path})", "",
            "회의 항목은 `회의 ID#발언 번호` 또는 정답 결정 ID, 질문은 `질문 ID(기대 상태)`. 원장 ID는 결과 JSON의 raw.id_map."]
    for name, tab in tabs.items():
        rows += ["", f"### {name} ({sum(len(v) for v in tab.values())}건)", "", "| 원인 | 건수 | 항목 |", "|---|---|---|"]
        rows += [f"| {k} | {len(v)} | {', '.join(v)} |" for k, v in sorted(tab.items(), key=lambda x: -len(x[1]))]
    print("\n".join(rows))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results_dev_baseline.json")
