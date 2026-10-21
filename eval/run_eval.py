"""Before / After 실측 — 같은 입력(회의 OPS-M03~OPS-M05, 추가 의견 O1~O5, 근거 질의 Q1~Q6)을 세 방식으로 돌린다.

  Before-A  LLM 단독         : 회의록·질문만 주고 한 번에 답하게 한다 (RAG 없음)
  Before-B  단순 RAG         : 4종 자료를 버전·대체 구분 없이 한꺼번에 검색해 붙여 준다 (흔한 RAG 구성)
  After     결정 원장 Agent  : 이 프로젝트 그래프 (우선순위 해석 + 권위 게이트 + 코드 판정표 + 검증)

사용: python eval/run_eval.py [--only after] [--limit-meetings OPS-M04]
결과: eval/results.json, eval/results.md
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.crosscheck import build_checkers  # noqa: E402
from agent.ledger_graph import LedgerDeps, build_ledger_graph  # noqa: E402
from ledger.llm import LocalLLM  # noqa: E402
from ledger.mcp_bridge import LedgerMCP  # noqa: E402
from ledger.rag import LedgerRAG  # noqa: E402

GT = json.loads((ROOT / "eval" / "ground_truth.json").read_text())
MEET = ROOT / "data" / "project" / "r1_meetings"
STALE_IDS = {"OPS-0003", "CSB-0001", "CSB-0003"}      # 대체된 결정 — 근거로 들면 '옛 결과 간섭'

# ------------------------------------------------------------------ 공통 출력 형식
# Before-A·B가 내야 하는 JSON 형식. After 결과도 run_after에서 같은 모양(seq·statement·quote·against)으로 바꿔 같은 채점기로 잰다
BASE_SCHEMA = {"type": "object", "properties": {
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "seq": {"type": "integer"}, "statement": {"type": "string"}, "quote": {"type": "string"},
        "conflicts_with": {"type": "array", "items": {"type": "string"}}},
        "required": ["seq", "statement", "quote", "conflicts_with"]}},
    "action_items": {"type": "array", "items": {"type": "object", "properties": {
        "seq": {"type": "integer"}, "statement": {"type": "string"},
        "owner": {"type": ["string", "null"]}, "due": {"type": ["string", "null"]}},
        "required": ["seq", "statement", "owner", "due"]}}},
    "required": ["decisions", "action_items"]}
OPINION_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["conflict", "allowed_with_waiver", "no_conflict", "needs_approval"]},
    "against": {"type": "array", "items": {"type": "string"}}}, "required": ["verdict", "against"]}

BASE_MEETING_SYS = ("회의록에서 결정(확정 발언 seq, 내용, 원문 인용 quote)과 할 일(seq, 담당자, 기한 YYYY-MM-DD, 없으면 null)을 뽑는다. "
                    "각 결정이 참고 자료의 결정·정책·문서와 충돌하면 conflicts_with에 그 ID를 쓴다.")
BASE_OPINION_SYS = ("새 의견이 참고 자료의 결정·정책·문서와 충돌하는지 판정한다. verdict: conflict / allowed_with_waiver(예외 범위 안) / "
                    "no_conflict / needs_approval. against에 근거 ID.")
BASE_QUESTION_SYS = "프로젝트 결정에 대한 질문에 참고 자료만으로 3~5문장 답한다. 결정 ID가 있으면 밝힌다. 모르면 모른다고 한다."


def seg_lines(mid: str) -> tuple[list[dict], str]:
    """
    회의 전사문 파일을 읽어 발언 목록과 프롬프트용 텍스트를 만든다
    Args:
        mid: 회의 ID (예: OPS-M04)
    Returns:
        (발언 목록, '[seq] 화자(역할): 발언' 줄을 이은 문자열)
    """
    m = json.loads((MEET / f"{mid}.json").read_text())
    return m["segments"], "\n".join(f"[{s['seq']}] {s['speaker']}({s['role']}): {s['text']}" for s in m["segments"])


def naive_context(rag: LedgerRAG, query: str, k: int = 3) -> str:
    """
    Before-B용 참고 자료 — 버전·상태 구분 없이 네 저장소에서 비슷한 것을 가져온다

    filtered=False·include_proposed=True로 대체된 결정·구버전 정책·승인 대기 결정까지 섞어
    흔한 RAG 구성에서 생기는 '옛 결과 간섭'을 재현한다.
    Args:
        rag: RAG 저장소
        query: 검색어
        k: 결정·정책 검색 수 (문서·회의 원문은 2건 고정)
    Returns:
        '[종류 ID] 내용' 줄을 이은 문자열
    """
    rows = []
    for d in rag.search_decisions(query, k=k, filtered=False, include_proposed=True):
        rows.append(f"[결정 {d['decision_id']}] {d.get('statement') or d.get('text', '')}")
    for r in rag.search_rules(query, k=k, filtered=False):
        rows.append(f"[정책 {r['rule_id']}] {r['text']}")
    for b in rag.search_baseline(query, k=2, filtered=False):
        rows.append(f"[문서 {b['doc_id']} {b['section']}] {b['text'][:200]}")
    for s in rag.search_transcript(query, k=2):
        rows.append(f"[회의 {s['meeting_id']}#{s['seq']}] {s['text']}")
    return "\n".join(rows)


# ------------------------------------------------------------------ 실행기
async def run_baseline(kind: str, llm: LocalLLM, rag: LedgerRAG, meetings, opinions, questions) -> dict:
    """
    Before 방식 실행 — 회의·의견·질문마다 LLM을 한 번씩 호출
    Args:
        kind: "A"(LLM 단독, 참고 자료 없음) 또는 "B"(단순 RAG)
        llm: 사용할 LLM
        rag: RAG 저장소 (kind="B"일 때만 사용)
        meetings: 회의 ID 목록
        opinions: ground_truth의 의견 목록
        questions: ground_truth의 질의 목록
    Returns:
        meetings·opinions·questions별 예측과 소요 시간·호출 수·토큰
    """
    out = {"meetings": {}, "opinions": {}, "questions": {}}
    for mid in meetings:
        _, lines = seg_lines(mid)
        ctx = naive_context(rag, lines[:1500], k=5) if kind == "B" else ""     # 회의록 앞부분을 검색어로
        t = time.perf_counter()
        prefix = f"## 참고 자료\n{ctx}\n\n" if ctx else ""
        r, u = await llm.json(BASE_MEETING_SYS, prefix + f"## 회의 {mid}\n{lines}", BASE_SCHEMA)
        out["meetings"][mid] = {"decisions": [{"seq": d["seq"], "statement": d["statement"], "quote": d["quote"],
                                               "against": d.get("conflicts_with", [])} for d in r.get("decisions", [])],
                                "action_items": r.get("action_items", []), "sec": time.perf_counter() - t, "calls": 1,
                                "tokens": u["tokens"]}
    for o in opinions:
        ctx = naive_context(rag, o["text"]) if kind == "B" else ""
        t = time.perf_counter()
        prefix = f"## 참고 자료\n{ctx}\n\n" if ctx else ""
        r, u = await llm.json(BASE_OPINION_SYS, prefix + f"## 의견\n{o['text']}", OPINION_SCHEMA)
        out["opinions"][o["id"]] = {**r, "sec": time.perf_counter() - t, "calls": 1, "tokens": u["tokens"]}
    for q in questions:
        ctx = naive_context(rag, q["text"]) if kind == "B" else ""
        t = time.perf_counter()
        ans, u = await llm.text(BASE_QUESTION_SYS, (f"## 참고 자료\n{ctx}\n\n" if ctx else "") + f"## 질문\n{q['text']}")
        out["questions"][q["id"]] = {"answer": ans, "sec": time.perf_counter() - t, "calls": 1, "tokens": u["tokens"]}
    return out


async def run_after(llm: LocalLLM, meetings, opinions, questions) -> dict:
    """
    After 방식 실행 — 결정 원장 그래프를 돌리고 결과를 채점 형식으로 변환

    fast·quality 모두 같은 LLM을 써서 Before와 모델 조건을 맞춘다.
    Args:
        llm: 사용할 LLM
        meetings: 회의 ID 목록
        opinions: ground_truth의 의견 목록
        questions: ground_truth의 질의 목록
    Returns:
        meetings·opinions·questions별 예측과 소요 시간·호출 수·토큰
    """
    mcp = LedgerMCP()
    await mcp.start()
    graph = build_ledger_graph(LedgerDeps(fast_llm=llm, quality_llm=llm, mcp=mcp, checkers=build_checkers()))
    out = {"meetings": {}, "opinions": {}, "questions": {}}

    async def go(msg):
        """
        그래프 1회 실행과 시간 측정
        Args:
            msg: 그래프 입력 문장
        Returns:
            (최종 상태, 소요 초)
        """
        t = time.perf_counter()
        st = await graph.ainvoke({"message": msg, "history": [], "events": []})
        return st, time.perf_counter() - t

    try:
        for mid in meetings:
            st, sec = await go(f"{mid} 회의 정리해줘")
            ds = []
            # against는 참고 사례(past_project)까지 전부, blocking은 실제로 막는 충돌만 — 오탐 채점은 blocking 기준
            for d in st["report"]["items"]:
                r = d["review"]
                ds.append({"seq": d["decided_seq"], "statement": d["statement"],
                           "quote": " ".join(e["quote"] for e in d["evidence"] if e["seq"] == d["decided_seq"]),
                           "against": [a for c in r["conflicts"] for a in c["against"]],
                           "blocking": [a for c in r["conflicts"] if c["type"] != "past_project" for a in c["against"]],
                           "changes": r.get("changes", []), "confirms": r.get("confirms", []), "status": r["status"]})
            actions = [{"seq": a["evidence"][0]["seq"] if a["evidence"] else None,
                        "statement": a["statement"], "owner": a.get("owner"), "due": a.get("due")}
                       for a in st["report"]["action_items"]]
            out["meetings"][mid] = {"decisions": ds, "action_items": actions,
                                    "sec": sec, "calls": st["usage"]["llm_calls"], "tokens": st["usage"]["tokens"],
                                    "events": st["events"]}
        for o in opinions:
            st, sec = await go(o["text"])
            it = st["report"]["items"][0]
            r = it["review"]
            blocking = [c for c in r["conflicts"] if c["type"] != "past_project"]
            # 그래프 판정(review)을 Before와 같은 4종 verdict로 맞춘다. 예외가 적용되고 막는 충돌이 없으면 allowed_with_waiver
            verdict = ("allowed_with_waiver" if r.get("waiver_applied") and not blocking else
                       "conflict" if r["status"] == "conflict" else
                       "needs_approval" if r["status"] == "needs_approval" else "no_conflict")
            out["opinions"][o["id"]] = {"verdict": verdict,
                                        "against": [a for c in r["conflicts"] for a in c["against"]],
                                        "waiver": r.get("waiver_applied"), "topic": it["topic"], "sec": sec,
                                        "calls": st["usage"]["llm_calls"], "tokens": st["usage"]["tokens"]}
        for q in questions:
            st, sec = await go(q["text"])
            out["questions"][q["id"]] = {"answer": st["final"], "sec": sec, "calls": st["usage"]["llm_calls"],
                                         "tokens": st["usage"]["tokens"],
                                         "attempts": st.get("validation_attempts", 1)}
    finally:
        await mcp.stop()
    return out


# ------------------------------------------------------------------ 채점
def locate(quote: str, segs: dict) -> int | None:
    """
    인용문이 실제로 있는 발언 번호 찾기

    '[4] 서지후(PL): ...' 같은 머리표는 떼고 비교한다 (방식 간 공정한 채점).
    Args:
        quote: 예측 결과의 인용문
        segs: 발언 번호 → 발언 원문
    Returns:
        일치하는 발언 번호. 6자 미만이거나 원문에 없으면 None
    """
    # 머리표 '[번호] 화자(역할): ' 제거. 괄호 역할 표기는 있을 수도 없을 수도 있다
    q = re.sub(r"^\[\d+\]\s*\S+?(\([^)]*\))?:\s*", "", (quote or "").strip())
    if len(q) < 6:                             # 너무 짧은 인용은 우연 일치가 많아 인정하지 않는다
        return None
    hits = [n for n, t in segs.items() if q in t or t in q]     # 발언 일부 인용과 여러 발언 이어 붙이기 둘 다 허용
    return hits[-1] if hits else None          # 여러 발언을 이어 붙였으면 마지막(확정) 발언


def norm_owner(o):
    """
    담당자 이름 정규화 — '장상민 (백엔드)'처럼 붙은 괄호 설명을 뗀다
    Args:
        o: 예측한 담당자
    Returns:
        괄호를 뗀 이름. 비어 있으면 None
    """
    return re.sub(r"\s*\(.*\)", "", o).strip() if o else None


def score(run: dict) -> dict:
    """
    한 방식의 실행 결과를 ground_truth로 채점

    회의: 결정 재현율·정밀도, 회상 발언 오추출, 인용 일치, 할 일 담당·기한, 충돌 탐지·오탐, 대체된 결정 인용.
    의견: verdict 일치. 질의: 기대 상태(미결·승인 대기) 표현 또는 기대 결정 ID 포함 여부.
    Args:
        run: run_baseline 또는 run_after 결과
    Returns:
        지표 이름 → 값 (분모가 0이면 None)
    """
    m = {"dec_gt": 0, "dec_hit": 0, "dec_pred": 0, "dec_pred_ok": 0, "quote_total": 0, "quote_exact": 0,
         "act_gt": 0, "act_ok": 0, "conf_gt": 0, "conf_hit": 0, "false_alarm": 0, "stale_cited": 0, "trap_hit": 0}
    for mid, pred in run["meetings"].items():
        g = GT["meetings"][mid]
        segs = {s["seq"]: s["text"] for s in seg_lines(mid)[0]}
        # 함정 중 '지난 결정을 회상하는 발언'만 — 이것을 결정으로 뽑으면 오추출
        traps = {t["seq"] for t in g.get("traps", []) if "회상" in t["why"]}
        m["dec_gt"] += len(g["decisions"])
        m["dec_pred"] += len(pred["decisions"])
        for d in pred["decisions"]:              # 발언 번호는 인용 위치로 확정 (번호를 틀리게 적은 경우 보정)
            d["_seq"] = locate(d.get("quote"), segs) or d["seq"]
        matched = {}               # 정답 결정 ID → 그 근거 발언을 짚은 첫 예측 (충돌 채점에 재사용)
        for gd in g["decisions"]:
            hit = next((d for d in pred["decisions"] if d["_seq"] in gd["evidence_seq"]), None)
            if hit:
                m["dec_hit"] += 1
                matched[gd["id"]] = hit
        gt_seqs = {s for gd in g["decisions"] for s in gd["evidence_seq"]}
        for d in pred["decisions"]:
            m["dec_pred_ok"] += d["_seq"] in gt_seqs
            m["trap_hit"] += d["_seq"] in traps
            m["quote_total"] += 1
            m["quote_exact"] += locate(d.get("quote"), segs) is not None
            m["stale_cited"] += len(STALE_IDS & set(d.get("against", [])))
        for ga in g["action_items"]:
            m["act_gt"] += 1
            # 할 일 짝짓기: 발언 번호가 맞으면 우선, 없으면 문장에 같은 단어가 하나라도 있는 것
            words = set(ga["statement"].split())
            pa = next((a for a in pred["action_items"] if a.get("seq") in ga["evidence_seq"]), None) or \
                next((a for a in pred["action_items"] if words & set((a.get("statement") or "").split())), None)
            m["act_ok"] += (bool(pa) and norm_owner(pa.get("owner")) == ga.get("owner")
                            and pa.get("due") == ga.get("due"))
        for gc in g["conflicts"]:
            if gc["type"] == "past_project":   # 과거 프로젝트 사례는 참고용이라 탐지 대상에서 제외
                continue
            m["conf_gt"] += 1
            d = matched.get(gc["target"])
            if d:
                ids = set(d.get("against", [])) | set(d.get("changes", []))   # 승인된 변경(changes)으로 짚어도 탐지로 인정
                m["conf_hit"] += bool(ids & set(gc["against"]))
        # 오탐: 막는 충돌이 없어야 할 결정에 충돌을 붙인 경우. Before는 blocking이 없어 against로 대신 본다
        conflict_targets = {gc["target"] for gc in g["conflicts"]
                            if gc["type"] not in ("past_project", "change_approved")}
        for gid, d in matched.items():
            if gid not in conflict_targets and d.get("blocking", d.get("against")):
                m["false_alarm"] += 1
    o = {"total": 0, "verdict_ok": 0, "stale_cited": 0}
    for gid, pred in run["opinions"].items():
        g = next(x for x in GT["opinions"] if x["id"] == gid)
        o["total"] += 1
        o["verdict_ok"] += pred.get("verdict") == g["expected"]
        o["stale_cited"] += len(STALE_IDS & set(pred.get("against", [])))
    q = {"total": 0, "ok": 0}
    for gid, pred in run["questions"].items():
        g = next(x for x in GT["questions"] if x["id"] == gid)
        a = pred["answer"]
        q["total"] += 1
        # 질의 정답 기준: 미결이면 '결정되지 않음' 류 표현, 승인 대기면 그 상태 언급, 나머지는 기대 결정 ID 포함
        if g.get("expected_state") == "undecided":
            ok = bool(re.search(r"결정되지 않|미결|확정되지|정해지지", a))
        elif g.get("expected_state") == "proposed":
            ok = "승인 대기" in a or "승인 전" in a
        else:
            ok = g["expected_decision"] in a
        if gid == "Q6":                          # 현재 기준 30초를 답해야 한다 (구버전 5초를 현재값으로 말하면 오답)
            ok = ok and "30초" in a
        q["ok"] += ok
    items = list(run["meetings"].values()) + list(run["opinions"].values()) + list(run["questions"].values())
    cost = {"avg_sec": round(sum(x["sec"] for x in items) / len(items), 1),
            "llm_calls": sum(x["calls"] for x in items), "tokens": sum(x["tokens"] for x in items)}

    def pct(a, b):
        return round(100 * a / b) if b else None

    return {
        "결정 재현율(%)": pct(m["dec_hit"], m["dec_gt"]), "결정 정밀도(%)": pct(m["dec_pred_ok"], m["dec_pred"]),
        "회상 발언 오추출(건)": m["trap_hit"], "인용 원문 일치(%)": pct(m["quote_exact"], m["quote_total"]),
        "할 일 담당·기한 정확(%)": pct(m["act_ok"], m["act_gt"]),
        "충돌 탐지율(%)": pct(m["conf_hit"], m["conf_gt"]), "충돌 오탐(건)": m["false_alarm"],
        "의견 판정 정확(%)": pct(o["verdict_ok"], o["total"]),
        "대체된 결정 인용(건)": m["stale_cited"] + o["stale_cited"],
        "근거 질의 정답(%)": pct(q["ok"], q["total"]),
        "평균 처리 시간(초)": cost["avg_sec"], "LLM 호출 수": cost["llm_calls"], "토큰": cost["tokens"],
        # 로컬 모델이라 실비용은 0원. 같은 토큰을 OpenAI gpt-4o-mini로 처리했다면 얼마인지 환산(입력·출력 혼합 단가 $0.3/1M 가정)
        "비용 환산(USD, gpt-4o-mini)": round(cost["tokens"] * 0.3 / 1_000_000, 4)}


def to_md(scores: dict) -> str:
    """
    방식별 점수를 Markdown 표로 변환 (행 = 지표, 열 = 방식)
    Args:
        scores: 방식 이름 → score() 결과
    Returns:
        Markdown 표 문자열. 값이 None이면 '-'
    """
    cols = list(scores)
    keys = list(next(iter(scores.values())))
    rows = ["| 지표 | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for k in keys:
        rows.append(f"| {k} | " + " | ".join("-" if scores[c][k] is None else str(scores[c][k]) for c in cols) + " |")
    return "\n".join(rows)


def isolated_store() -> None:
    """
    평가용 격리 저장소 준비 — 평가는 시연 서버와 원장을 공유하지 않는다

    store/chroma를 임시 폴더로 복사하고 원장(SQLite)을 그 폴더에 시드 상태로 새로 만든다.
    MCP 서버 자식 프로세스도 LEDGER_STORE 환경변수를 물려받아 같은 사본을 쓴다.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ledger_eval_"))
    shutil.copytree(ROOT / "store" / "chroma", tmp / "chroma")
    os.environ["LEDGER_STORE"] = str(tmp)
    import ledger.store as st  # pylint: disable=import-outside-toplevel  # LEDGER_STORE를 바꾼 뒤에 읽어야 한다
    importlib.reload(st)       # 저장 경로를 import 시점에 정하므로 바뀐 LEDGER_STORE로 다시 읽힌다
    st.LedgerStore().load_seed()


async def main(only: str | None, meetings: list[str]):
    """
    선택한 방식을 실행하고 전체를 다시 채점해 results.json·results.md로 저장

    실행하지 않은 방식은 기존 results.json의 결과를 그대로 두고 함께 채점한다.
    Args:
        only: 실행할 방식 하나 (before-a / before-b / after / rescore). None이면 전부, rescore면 실행 없이 재채점만
        meetings: 평가할 회의 ID 목록
    """
    isolated_store()
    llm = LocalLLM("qwen3:8b")
    rag = LedgerRAG(path=Path(__import__("os").environ["LEDGER_STORE"]) / "chroma")
    ops, qs = GT["opinions"], GT["questions"]
    path = ROOT / "eval" / "results.json"
    runs = json.loads(path.read_text())["runs"] if path.exists() else {}
    if only == "rescore":
        pass
    if only in (None, "before-a"):
        runs["Before-A LLM 단독"] = await run_baseline("A", llm, rag, meetings, ops, qs)
    if only in (None, "before-b"):
        runs["Before-B 단순 RAG"] = await run_baseline("B", llm, rag, meetings, ops, qs)
    if only in (None, "after"):
        runs["After 결정 원장"] = await run_after(llm, meetings, ops, qs)
    order = [k for k in ("Before-A LLM 단독", "Before-B 단순 RAG", "After 결정 원장") if k in runs]
    scores = {k: score(runs[k]) for k in order}
    path.write_text(json.dumps({"runs": runs, "scores": scores}, ensure_ascii=False, indent=1, default=str))
    md = ("# Before / After 실측\n\n같은 입력: 회의 " + ", ".join(meetings) + f", 추가 의견 {len(ops)}건, 근거 질의 {len(qs)}건. "
          "모델은 모두 로컬 qwen3:8b (think off, temperature 0).\n\n" + to_md(scores) + "\n")
    (ROOT / "eval" / "results.md").write_text(md)
    print(md)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["before-a", "before-b", "after", "rescore"])
    ap.add_argument("--meetings", default="OPS-M03,OPS-M04,OPS-M05")
    args = ap.parse_args()
    asyncio.run(main(args.only, args.meetings.split(",")))
