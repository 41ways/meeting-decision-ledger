"""근거 질문의 후보 선택 — 색인으로 좁히고, LLM은 좁힌 목록에서 관련 ID만 고른다 (docs/설계_재구성안.md §8).

순서
  1. 범위: 질문 프로젝트 · 질문의 기간 표현 · 입력창에서 고른 회의
  2. 색인: 별칭이 가리킨 주제(없으면 plan이 고른 주제) · 질문에 적힌 결정 ID·회의 ID로 낱말 색인을 찾는다.
     하나도 없으면 질문 내용어로 한 번 더 (ledger/term_index.find_candidates)
  3. 읽기: 후보가 CANDIDATE_LIMIT(50)건 이내면 LLM이 그 목록만 읽고 ID를 고른다

벡터 검색은 결정·미결 후보에 쓰지 않는다. 점수로 붙이면 "Redis 캐시 쓰기로 했어?"가 Redis를 기각한
대화 저장소 결정으로 새고, 왜 붙었는지 설명할 수 없다. 색인이 비는 질문은 아래 전체 읽기로 드러나게 처리한다.

전체 읽기 대비책 (프로젝트 기록 전체를 읽고, 답 앞에 코드가 알림을 붙인다 · 이벤트 FullReadFallback)
  - 색인 후보가 0건인데 질문이 알려진 주제·프로젝트·ID를 가리킬 때
  - 별칭 충돌로 후보가 CANDIDATE_LIMIT를 넘을 때
  - 사용자가 '전부 찾아봐'처럼 요청했을 때
  목록이 LIST_TOKEN_BUDGET을 넘으면 나눠 읽고 고른 ID를 합친다.

LLM은 ID만 고르고 문장을 쓰지 않으며, 고른 ID가 후보에 없으면 버린다. "해당 없음"도 고를 수 있다.
고른 결과는 주제로 찾은 결과(resolve_current)와 대조한다. 둘이 다른 결정을 가리키면 단정하지 않고 '확인 필요'.
"""
from __future__ import annotations

import re
import time

from ledger import aliases
from ledger.term_index import DECISION_ID
from ledger.thresholds import CANDIDATE_LIMIT, LIST_TOKEN_BUDGET

PICK_SCHEMA = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
               "required": ["ids"]}

PICK_SYSTEM = (
    "질문에 답하는 데 직접 필요한 후보의 ID만 고른다. 질문과 다른 사안이면 문장이 비슷해도 고르지 않는다.\n"
    "같은 사안의 결정이 바뀐 이력이 있으면 지금 것과 이전 것을 함께 고른다. 미결(O:)도 같은 사안이면 고른다.\n"
    "관련 후보가 하나도 없으면 빈 목록.")

STATUS_KO = {"approved": "확정", "proposed": "승인 대기", "rejected": "반려"}

# 사용자가 색인을 건너뛰고 전부 읽으라고 한 표현
FULL_READ_Q = re.compile(r"전부\s*(찾|읽|봐|확인)|전체\s*(를\s*)?(찾|읽|봐|확인|기록)|다\s*찾아|모두\s*(찾|읽|확인)|빠짐없이")
MEETING_REF = re.compile(r"(?<![A-Za-z0-9])([A-Z]{3})-([MI]\d{2})(?!\d)")


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
    # 미결 요약은 짧아서('캐시 대상 정의') 질문 표현과 안 맞기 쉽다 — 근거 발언 앞부분을 함께 보여 준다
    quote = " / ".join((e.get("quote") or "")[:40] for e in (c.get("evidence") or [])[:2] if e.get("quote"))
    return (f"{key(c)} [미결] {c.get('date', '')} · {c.get('topic', '')} · {c['statement']}"
            + (f" (발언: {quote})" if quote else ""))


def key(c: dict) -> str:
    """
    후보의 선택 키 (D:결정 ID 또는 O:회의 ID-발언 번호)
    Args:
        c: 후보
    Returns:
        키 문자열
    """
    if c["kind"] == "decision":
        return "D:" + c["decision_id"]
    # 항목 ID의 쌍점(OPS-M03:open:10)은 모델이 'O:OPS-M03'처럼 잘라 돌려줘서 버려졌다 — 쌍점 없는 키로 보인다
    return "O:" + c["item_id"].replace(":open:", "-")


PICKED_KEY = re.compile(r"[DO]:[A-Z]{3}-[A-Z0-9-]+")


def pick_key(raw) -> str:
    """
    모델이 돌려준 선택에서 후보 키만 뽑는다

    모델이 ID 대신 후보 줄을 통째로 돌려주기도 한다('D:OPS-0008 [확정] … 대화 이력은 …').
    맨 앞의 키만 쓰고, 그래도 후보에 없는 키면 호출 측이 버린다.
    Args:
        raw: 모델 출력의 ids 원소
    Returns:
        키 (D:OPS-0008, O:OPS-M03-10). 키 모양이 없으면 빈 문자열
    """
    m = PICKED_KEY.match(raw.strip()) if isinstance(raw, str) else None
    return m.group(0) if m else ""


def est_tokens(text: str) -> int:
    """
    목록 글의 토큰 수 어림 (qwen3 토크나이저에서 한글·영문 섞인 줄이 대략 1.6자당 1토큰)
    Args:
        text: 글
    Returns:
        어림 토큰 수
    """
    return int(len(text) / 1.6) + 1


def chunks(lines: list[str], budget: int = LIST_TOKEN_BUDGET) -> list[list[str]]:
    """
    목록을 토큰 상한 안에서 나눈다 (순서 유지)
    Args:
        lines:  후보 줄
        budget: 한 번에 보낼 토큰 상한
    Returns:
        줄 묶음 목록 (빈 목록이면 빈 목록)
    """
    out, cur, size = [], [], 0
    for ln in lines:
        t = est_tokens(ln) + 1
        if cur and size + t > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += t
    return out + ([cur] if cur else [])


async def read_list(llm, question: str, cands: list[dict], budget: int = LIST_TOKEN_BUDGET) -> tuple[list[dict], list]:
    """
    후보 목록을 LLM이 읽고 관련 ID를 고른다 (상한을 넘으면 나눠 읽고 합친다)
    Args:
        llm:      선택 모델
        question: 질문
        cands:    후보
        budget:   한 번에 보낼 토큰 상한
    Returns:
        (고른 후보, LLM 사용량 목록)
    """
    keys = {key(c): c for c in cands}
    chosen, usages = [], []
    for part in chunks([_line(c) for c in cands], budget):
        out, usage = await llm.json(PICK_SYSTEM, f"## 질문\n{question}\n\n## 후보\n" + "\n".join(part),
                                    PICK_SCHEMA, max_tokens=200)
        usages.append(usage)
        chosen += [keys[k] for k in (pick_key(i) for i in out.get("ids", [])) if k in keys]
    seen, uniq = set(), []
    for c in chosen:
        if key(c) not in seen:
            seen.add(key(c))
            uniq.append(c)
    return uniq, usages


def full_read_reason(question: str, n_found: int, known: bool) -> str:
    """
    전체 읽기로 갈 까닭 (가지 않으면 빈 문자열)
    Args:
        question: 질문
        n_found:  색인 후보 수
        known:    질문이 알려진 주제·프로젝트·ID를 가리키는가
    Returns:
        "요청" | "충돌" | "색인 0건" | ""
    """
    if FULL_READ_Q.search(question):
        return "요청"
    if n_found > CANDIDATE_LIMIT:
        return "충돌"
    if n_found == 0 and known:
        return "색인 0건"
    return ""


def full_read_notice(reason: str, project_key: str, n_read: int, n_found: int, sec: float,  # pylint: disable=too-many-arguments,too-many-positional-arguments
                     parts: int) -> str:
    """
    전체 읽기 알림 문구 (답 앞에 코드가 붙인다)
    Args:
        reason:      full_read_reason 결과
        project_key: 프로젝트 약자 (OPS)
        n_read:      읽은 기록 수
        n_found:     색인 후보 수
        sec:         읽는 데 걸린 초
        parts:       나눠 읽은 횟수
    Returns:
        알림 한 줄
    """
    head = {"요청": "요청에 따라", "충돌": f"색인 후보가 {n_found}건으로 많아(별칭 충돌)",
            "색인 0건": "색인에서 찾지 못해"}[reason]
    split = f" · {parts}번에 나눠 읽음" if parts > 1 else ""
    return f"※ {head} {project_key} 기록 전체 {n_read}건을 읽었습니다 · 약 {sec:.0f}초{split}"


async def pick(deps, question: str, project_id: str, *, topic: str = "", scope_meeting: str = "",  # pylint: disable=too-many-locals,too-many-arguments
               period: tuple[str, str] | None = None, project_explicit: bool = False, project_key: str = "") -> dict:
    """
    색인으로 후보를 좁히고 LLM이 관련 ID만 고르게 한다 (필요할 때만 전체 읽기)
    Args:
        deps:             의존성 (mcp, quality_llm)
        question:         사용자 질문
        project_id:       질문 대상 프로젝트
        topic:            plan이 정한 주제 키 (별칭이 없을 때 색인에 쓴다)
        scope_meeting:    입력창에서 고른 회의 ID
        period:           질문 속 기간 (시작일, 종료일)
        project_explicit: 프로젝트를 사용자가 고르거나 질문에서 판단했는가 (기본값이 아님)
        project_key:      알림에 쓸 프로젝트 약자
    Returns:
        {"decisions", "opens", "shown", "usages", "how": "색인" | "전체" | "없음", "tier", "via": {키: 일치 설명},
         "topics": 별칭 일치, "full_read": 전체 읽기 정보 또는 None}
    """
    hits = aliases.topics_of(question)
    topics = [h["topic"] for h in hits] or ([topic] if topic else [])
    ids = list(dict.fromkeys(DECISION_ID.findall(question)))
    mids = list(dict.fromkeys([f"{a}-{b}" for a, b in MEETING_REF.findall(question)]
                              + ([scope_meeting] if scope_meeting else [])))
    found = await deps.mcp.call("index_candidates", project_id=project_id, topics=topics, decision_ids=ids,
                                meeting_ids=mids, words=aliases.content_words(question),
                                date_from=(period or ("", ""))[0], date_to=(period or ("", ""))[1])
    cands = found["candidates"]
    via = {key(c): c.get("via", []) for c in cands}
    base = {"tier": found.get("tier", ""), "via": via, "topics": hits, "period_dropped": found.get("period_dropped")}
    reason = full_read_reason(question, len(cands), bool(topics or ids or mids or project_explicit))
    full = None
    t0 = time.perf_counter()
    if reason:
        reading = await deps.mcp.call("project_records_all", project_id=project_id)
        chosen, usages = await read_list(deps.quality_llm, question, reading)
        sec = time.perf_counter() - t0
        parts = len(usages)
        full = {"reason": reason, "read": len(reading), "found": len(cands), "parts": parts, "sec": round(sec, 1),
                "notice": full_read_notice(reason, project_key or project_id, len(reading), len(cands), sec, parts)}
        shown, how = len(reading), "전체"
    elif cands:
        chosen, usages = await read_list(deps.quality_llm, question, cands)
        shown, how = len(cands), "색인"
    else:
        chosen, usages, shown, how = [], [], 0, "없음"
    return {**base, "decisions": [c for c in chosen if c["kind"] == "decision"],
            "opens": [c for c in chosen if c["kind"] == "open"], "shown": shown, "usages": usages, "how": how,
            "full_read": full}


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
    # 주제로 찾은 승인 대기 결정 중 질문에 맞는 후보로 고르지 않은 것은 답의 근거에서 뺀다
    # (같은 주제라는 이유만으로 무관한 승인 대기 건을 대며 '아직 승인 대기'라고 답하던 오류)
    if picked.get("shown"):
        chosen = {d["decision_id"] for d in picked["decisions"]}
        res["pending_unpicked"] = [p for p in res.get("pending", []) if p["decision_id"] not in chosen]
        res["pending"] = [p for p in res.get("pending", []) if p["decision_id"] in chosen]
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
