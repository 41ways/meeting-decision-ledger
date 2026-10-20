"""근거 질문의 후보 선택 — 후보를 넓게 찾고, LLM은 관련 ID만 고른다 (docs/설계_재구성안.md §4.4).

벡터 점수만으로는 "캐시 쓰기로 했어?" 같은 질문이 문장이 비슷한 다른 사안(Redis를 기각한 대화 저장소)으로 붙는다.
그래서 후보를 넉넉히(결정 상위 12건 + 프로젝트의 미결) 모은 뒤, LLM이 질문에 실제로 답하는 데 필요한 ID만 고르게 한다.
LLM은 ID만 고르고 문장을 쓰지 않으며, 고른 ID가 후보에 없으면 버린다. "해당 없음"도 고를 수 있다.

고른 결과는 주제로 찾은 결과(resolve_current)와 대조한다. 둘이 다른 결정을 가리키면 단정하지 않고 '확인 필요'.
"""
from __future__ import annotations

PICK_SCHEMA = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
               "required": ["ids"]}

PICK_SYSTEM = (
    "질문에 답하는 데 직접 필요한 후보의 ID만 고른다. 질문과 다른 사안이면 문장이 비슷해도 고르지 않는다.\n"
    "같은 사안의 결정이 바뀐 이력이 있으면 지금 것과 이전 것을 함께 고른다. 미결(O:)도 같은 사안이면 고른다.\n"
    "관련 후보가 하나도 없으면 빈 목록.")

STATUS_KO = {"approved": "확정", "proposed": "승인 대기", "rejected": "반려"}


def _line(c: dict) -> str:
    """
    후보 한 줄 (ID · 상태 · 프로젝트 · 주제 · 문장)
    Args:
        c: 후보 (decision 또는 open)
    Returns:
        LLM에 보여 줄 줄
    """
    if c["kind"] == "decision":
        st = STATUS_KO.get(c.get("status"), c.get("status"))
        if c.get("status") == "approved" and not c.get("active"):
            st = "대체됨"
        return f"D:{c['decision_id']} [{st}] {c.get('project_id', '')} · {c.get('topic', '')} · {c['statement']}"
    return f"O:{c['item_id']} [미결] {c.get('date', '')} · {c.get('topic', '')} · {c['statement']}"


async def pick(deps, question: str, project_id: str) -> dict:
    """
    후보를 넓게 모으고 LLM이 관련 ID만 고르게 한다
    Args:
        deps:       의존성 (mcp, quality_llm)
        question:   사용자 질문
        project_id: 질문 대상 프로젝트 (미결 후보 범위)
    Returns:
        {"decisions": 고른 결정 후보, "opens": 고른 미결, "shown": 보여 준 후보 수, "usage": 사용량}
    """
    decs = [{**d, "kind": "decision"} for d in
            await deps.mcp.call("search_decisions", query=question, k=12, include_proposed=True)]
    opens = [{**o, "kind": "open"} for o in
             (await deps.mcp.call("open_items", project_id=project_id, include_linked=True))[-30:]]
    cands = decs + opens
    if not cands:
        return {"decisions": [], "opens": [], "shown": 0, "usage": None}
    keys = {("D:" + c["decision_id"]) if c["kind"] == "decision" else ("O:" + c["item_id"]): c for c in cands}
    out, usage = await deps.quality_llm.json(PICK_SYSTEM, f"## 질문\n{question}\n\n## 후보\n"
                                             + "\n".join(_line(c) for c in cands), PICK_SCHEMA, max_tokens=200)
    chosen = [keys[i.strip()] for i in out.get("ids", []) if isinstance(i, str) and i.strip() in keys]
    return {"decisions": [c for c in chosen if c["kind"] == "decision"],
            "opens": [c for c in chosen if c["kind"] == "open"], "shown": len(cands), "usage": usage}


def merge(res: dict, picked: dict, project_id: str) -> tuple[dict, list[dict], list[str]]:
    """
    고른 결과를 주제로 찾은 결과(res)에 합치고, 둘이 어긋나면 확인 필요 사유를 남긴다

    - 고른 것 중 질문 프로젝트의 확정 결정이 있으면 그것이 현재 기준 (주제 결과와 다르면 확인 필요)
    - 고른 확정 결정이 없는데 주제 결과에만 확정 결정이 있으면 그대로 쓰되 확인 필요
    - 고른 승인 대기 결정은 pending에, 다른 프로젝트 확정 결정은 advisory에 더한다
    - 고른 미결은 원문 논의(discussion) 형식으로 돌려준다
    Args:
        res:        resolve_current 결과
        picked:     pick 결과
        project_id: 질문 대상 프로젝트
    Returns:
        (합친 res, 미결에서 온 원문 논의 목록, 확인 필요 사유 목록)
    """
    res, why = dict(res), []
    approved = [d for d in picked["decisions"] if d.get("status") == "approved" and d.get("active")]
    mine = [d for d in approved if d.get("project_id") == project_id]
    topic_cur = res.get("current") if res.get("authority") == "decision" else None
    if mine:
        top = mine[0]
        if topic_cur and topic_cur["decision_id"] != top["decision_id"] and \
                topic_cur["decision_id"] not in {d["decision_id"] for d in mine}:
            why.append(f"주제로 찾은 결정 {topic_cur['decision_id']}와 질문에 맞춰 고른 결정 {top['decision_id']}가 다름")
        if not topic_cur or topic_cur["decision_id"] != top["decision_id"]:
            res["current"], res["authority"] = {**top, "_picked": True}, "decision"
    elif topic_cur and picked.get("shown"):
        why.append(f"주제로 찾은 결정 {topic_cur['decision_id']}를 질문에 맞는 후보로 고르지 않음")
    seen = {p["decision_id"] for p in res.get("pending", [])}
    res["pending"] = res.get("pending", []) + [d for d in picked["decisions"] if d.get("status") == "proposed"
                                               and d.get("project_id") == project_id and d["decision_id"] not in seen]
    seen = {a["decision_id"] for a in res.get("advisory", [])}
    res["advisory"] = res.get("advisory", []) + [d for d in approved if d.get("project_id") != project_id
                                                 and d["decision_id"] not in seen]
    disc = []
    for o in picked["opens"]:
        fallback = [{"meeting_id": o["meeting_id"], "seq": o.get("seq"), "speaker": "", "quote": o["statement"]}]
        for e in o.get("evidence") or fallback:
            disc.append({"meeting_id": e.get("meeting_id"), "seq": e.get("seq"), "speaker": e.get("speaker") or "",
                         "text": e.get("quote") or o["statement"], "open_item": o["item_id"], "state": o.get("state"),
                         "linked_to": o.get("linked_to")})
    if disc and res.get("authority") in ("none", "discussion"):
        res["authority"] = "discussion"
    return res, disc, why


def answer_label(item: dict) -> str:
    """
    근거 질문 답의 라벨 (확정 · 승인 대기 · 미결 · 확인 필요 · 찾지 못함)
    Args:
        item: research·discussion·check_reasons가 붙은 질의 항목
    Returns:
        라벨
    """
    res = item["research"]
    if item.get("check_reasons") or res.get("topic_doubt") or res.get("similar_other"):
        return "확인 필요"
    if res.get("authority") == "decision":
        return "확정"
    if res.get("pending"):
        return "승인 대기"
    if item.get("discussion"):
        return "미결"
    if res.get("authority") == "baseline":
        return "확정"            # 결정은 없지만 현행 기준 문서가 정한 값
    return "찾지 못함"
