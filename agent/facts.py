"""사실 질문 — "언제 회의야?", "참석자 누구야?", "누가 승인했어?" (docs/설계_재구성안.md §3.3).

날짜·사람·상태는 정확한 사실이라 벡터 검색이나 LLM을 쓰지 않고 회의 목록·원장·이력 표를 조회해 코드가 답한다.
'이거', '그 결정'처럼 대상을 가리키는 말은 LLM이 추측하지 않고, 직전 답이 가리킨 대상(context_refs)을 코드가 쓴다.
대상이 없으면 되묻는다.
"""
from __future__ import annotations

import re

from agent.enrich import LEADS

FACT_Q = re.compile(r"참석|참가자|참여자|누가\s*(승인|결정|정했|참석|있었|제안|제시|꺼냈|낸)|승인(자|한\s*사람|했어|한\s*거|한거|받았)"
                    r"|제안(자|한\s*사람)|처음\s*(말|꺼)"
                    r"|언제\s*(회의|정했|결정|승인)|무슨\s*회의|어느\s*회의|회의\s*날짜|며칠\s*(에|날)?\s*(회의|정했|결정|승인)")
DECISION_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}-\d{4}(?!\d)")
MEETING_REF = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}-[MI]\d{2}(?!\d)")


def is_fact_question(msg: str) -> bool:
    """
    사실 질문인지 (회의 날짜·참석자·결정자·승인자)
    Args:
        msg: 사용자 메시지
    Returns:
        사실 질문이면 True
    Examples:
        is_fact_question("이거 누가 승인한거야?") -> True
        is_fact_question("SSE는 왜 골랐어?") -> False
    """
    return bool(FACT_Q.search(msg))


def _status_label(d: dict) -> str:
    """
    원장 상태를 사용자 라벨로
    Args:
        d: 원장 결정
    Returns:
        확정 / 대체됨(→ID) / 승인 대기 / 반려
    """
    if d["status"] == "approved":
        return f"대체됨 → {d['superseded_by']}" if d.get("superseded_by") else "확정"
    return {"proposed": "승인 대기", "rejected": "반려"}.get(d["status"], d["status"])


def _people(meeting: dict) -> str:
    """
    참석자 줄 ('이름(역할)' 나열)
    Args:
        meeting: 회의 정보
    Returns:
        참석자 문자열
    """
    return " · ".join(f"{a['name']}({a['role']})" if a.get("role") else a["name"] for a in meeting.get("attendees", []))


async def _meeting_lines(deps, mid: str) -> list[str]:
    """
    회의 정보 줄 (날짜·제목·참석자·불참한 팀장·원문 파기 여부)
    Args:
        deps: 의존성 (mcp)
        mid:  회의 ID
    Returns:
        마크다운 줄 목록
    """
    m = await deps.mcp.call("meeting_info", meeting_id=mid)
    if not m:
        return [f"- 회의 {mid}: 회의 목록에 없음"]
    names = {a["name"] for a in m.get("attendees", [])}
    rows = [f"- 회의: {mid} {m.get('title', '')} · {m.get('date', '')}", f"- 참석: {_people(m)}"]
    absent = [n for n in LEADS if names and n not in names]
    if absent:
        rows.append(f"- 불참: {', '.join(absent)}(팀장)")
    if m.get("transcript_deleted"):
        rows.append("- 원문: 보존 기한이 지나 파기됨 (회의 정보와 결정 근거 인용만 남음)")
    return rows


def resolve_target(msg: str, refs: list[str]) -> tuple[str, str]:
    """
    질문이 가리키는 대상 — 메시지에 적힌 ID가 먼저, 없으면 직전 답이 가리킨 대상
    Args:
        msg:  사용자 메시지
        refs: 직전 답이 가리킨 결정·회의 ID (앞이 더 중요)
    Returns:
        (결정 ID 또는 "", 회의 ID 또는 "")
    """
    did = (DECISION_ID.findall(msg) or [""])[0]
    mid = (MEETING_REF.findall(msg) or [""])[0]
    if not did and not mid:
        did = next((r for r in refs if DECISION_ID.fullmatch(r)), "")
        mid = next((r for r in refs if MEETING_REF.fullmatch(r)), "")
    return did, mid


async def fact_answer(deps, msg: str, refs: list[str]) -> tuple[str, list[str]]:
    """
    사실 질문에 표 조회로 답한다 (LLM 없음)

    결정이 대상이면 상태·결정자·승인자·제안 이력·권한·근거 회의·이전 논의를,
    회의가 대상이면 회의 정보와 그 회의에서 나온 결정 후보를 보여 준다.
    Args:
        deps: 의존성 (mcp)
        msg:  사용자 메시지
        refs: 직전 답이 가리킨 대상
    Returns:
        (답 마크다운, 이 답이 가리킨 대상)
    """
    did, mid = resolve_target(msg, refs)
    if not did and not mid:
        return ("어떤 결정이나 회의를 말씀하시는지 알려 주세요. 예: \"OPS-0007 누가 승인했어?\", "
                "\"OPS-M04 참석자 누구야?\""), []
    rows, out_refs = [], []
    if did:
        h = await deps.mcp.call("decision_history", decision_id=did)
        d = h.get("decision")
        if not d:
            return f"{did}는 원장에 없는 결정입니다.", []
        label = _status_label(d)
        rows.append(f"[{label}] **{did}** · {d['statement'].rstrip('.')}")
        rows.append(f"- 회의에서 정한 사람: {d.get('decided_by') or '?'}")
        # 근거의 첫 발언이 확정 발언보다 앞이면 그 발언이 제안 — 아이디어를 꺼낸 사람에게 다시 물어볼 수 있게
        ev = sorted((e for e in d.get("evidence", []) if e.get("seq") is not None), key=lambda e: e["seq"])
        if len(ev) > 1 and ev[0].get("speaker"):
            rows.append(f"- 처음 제안한 사람: {ev[0]['speaker']} ({ev[0].get('meeting_id', '')} [{ev[0]['seq']}] "
                        f"\"{(ev[0].get('quote') or '')[:40]}…\")")
        if d["status"] == "approved":
            rows.append(f"- 승인: {d.get('approved_by') or '?'} · 시행 {d.get('effective_from') or '?'}")
        elif d["status"] == "proposed":
            prop = next((e for e in h.get("events", []) if e["kind"] == "decision_proposed"), None)
            rows.append("- 승인: 아직 승인 전" + (f" · 승인 요청 {prop['actor']} ({prop['at'][:10]})" if prop else ""))
            rows.append(f"- 승인할 사람: 팀장 {', '.join(LEADS)} (원장 기록은 팀장 승인)")
            pre = await deps.mcp.call("will_supersede", decision_id=did)
            rows.append("- 승인하면 대체될 결정: " + (", ".join(f"{x['decision_id']} {x['statement']}" for x in pre) or "없음"))
        else:
            rows.append(f"- {d.get('approved_by') or '반려'}")
        edits = [e for e in h.get("events", []) if e["kind"] == "decision_edited"]
        if edits:
            rows.append(f"- 승인 전 수정: {edits[-1]['actor']} ({edits[-1]['at'][:10]})")
        ev_mids = list(dict.fromkeys(e["meeting_id"] for e in d.get("evidence", []) if e.get("meeting_id")))
        mid = mid or (ev_mids[0] if ev_mids else "")
        out_refs.append(did)
    if mid:
        rows += await _meeting_lines(deps, mid)
        out_refs.append(mid)
    if did:
        # 이 결정으로 이어진 앞선 미결 = 이전 논의
        prior = [i for i in h.get("items", []) if i["kind"] == "open"]
        for i in prior:
            m = await deps.mcp.call("meeting_info", meeting_id=i["meeting_id"])
            rows.append(f"- 이전 논의: {i['meeting_id']} {m.get('title', '')} · {m.get('date', '')} · 미결 '{i['statement']}'"
                        + (f" · 참석 {_people(m)}" if m else ""))
    elif mid:
        # 회의가 대상이면 그 회의에서 나온 결정 후보와 라벨
        decs = [i for i in await deps.mcp.call("meeting_items", meeting_id=mid) if i["kind"] == "decision"]
        rows += [f"- 결정 [{i.get('label') or '?'}] {i['statement']}"
                 + (f" → {i['decision_id']}" if i.get("decision_id") else "")
                 for i in decs]
    return "\n".join(rows), out_refs
