"""일정 추가(schedule_add) 경로의 그래프 노드와 일정 조회 보조 함수.

route ─ schedule_add ─ schedule_add_node → END  (초안만 만든다. 저장은 화면의 '추가' → POST /events)

해석은 ledger.schedule_parse(날짜·시간 규칙 + LLM 추출 검증), 겹침·저장소는 ledger.calendar.
그래프는 deps.calendar(CalendarProvider)만 보므로 테스트에서 가짜 저장소로 바꿔 끼울 수 있다.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from langsmith import traceable

from ledger.calendar import find_conflicts
from ledger.schedule_parse import SCHEDULE_ADD_SCHEMA, add_permission, build_draft

DAYS = "월화수목금토일"
EXTRACT_SYS = ("일정 추가 문장에서 제목·참석자·프로젝트만 뽑아 JSON으로 답한다. 날짜·시간은 뽑지 않는다. "
               "title: 문장에 있는 말로만 짧게 (날짜·시간·사람 이름·'잡아줘' 같은 동사는 빼고). "
               "attendees: 문장에 나온 사람 이름만, 없으면 빈 목록. "
               "project: 문장에 OPS·CSB·HBE 약자나 프로젝트 이름이 있을 때만 그 약자, 없으면 빈 문자열.")


def events_as_meetings(calendar: Any, date_from: str, date_to: str) -> list[dict]:
    """
    저장된 일정을 회의 목록과 같은 모양으로 바꿔 일정 조회 답변에 합친다
    Args:
        calendar: CalendarProvider. None이면 빈 목록
        date_from: 시작일 (YYYY-MM-DD)
        date_to: 종료일 (YYYY-MM-DD, 포함)
    Returns:
        meeting_id(=일정 ID)·date·title·attendees·domain(meeting|interview|event)·time 목록
    """
    if calendar is None:
        return []
    out = []
    for e in calendar.list_events(date_from, date_to):
        span = f"{e['start']}~{e['end']}" if e.get("end") else e.get("start", "")
        out.append({"meeting_id": e.get("event_id") or e.get("external_id") or "외부", "date": e["date"],
                    "title": e["title"], "attendees": e.get("attendees") or [], "domain": e.get("kind") or "event",
                    "time": span, "start": e.get("start", "")})
    return out


def render_draft(d: dict) -> str:
    """
    일정 초안을 채팅 답변 문장으로
    Args:
        d: 초안 (build_draft 결과 + conflicts·permission)
    Returns:
        답변 문자열 — 해석 결과 요약, 겹침 경고, 되물을 항목
    """
    asks = {"date": "날짜", "start": "시작 시간", "title": "제목"}
    if d["date"]:
        wd = DAYS[date.fromisoformat(d["date"]).weekday()]
        when = f"{d['date'][5:]}({wd})" + (f" {d['start']}" if d["start"] else "") + (f"~{d['end']}" if d["end"] else "")
    else:
        when = "날짜 미정"
    lines = [f"일정 추가 초안입니다 — {when} {d['title'] or '(제목 없음)'}. "
             "아래 카드에서 확인하고 '추가'를 눌러야 저장됩니다."]
    if d["missing"]:
        lines.append("- 되물음: 비어 있는 항목 — " + ", ".join(asks[k] for k in d["missing"]) + ". 알려 주시거나 카드에서 바로 채워 주세요.")
    for c in d.get("conflicts", []):
        span = f"{c['start']}~{c['end']}" if c["start"] else "시간 미상"
        lines.append(f"- 겹침: {', '.join(c['who'])} — {c['id']} {c['title']} ({span})")
    lines += [f"- 확인: {w}" for w in d["warnings"] + d["notes"]]
    if d.get("permission") != "add":
        lines.append(f"- 권한: {d.get('permission_note')}")
    return "\n".join(lines)


@traceable(name="schedule_add_node")
async def schedule_add_node(state: dict, deps: Any) -> dict:
    """
    일정 추가 요청을 초안으로 만드는 노드 — 저장하지 않는다

    날짜·시간은 코드가 먼저 해석하고, LLM은 제목·참석자·프로젝트만 뽑는다(원문 대조로 검증).
    LLM이 실패해도 코드 해석만으로 초안을 만든다. 겹침은 같은 날 일정·회의에서 같은 참석자를 찾는다.
    Args:
        state: 그래프 상태 (message, user_id, scope_project, usage, events 사용)
        deps: 의존성 (fast_llm, mcp, calendar, today)
    Returns:
        schedule_draft(초안), final(답변), usage, events(ScheduleDrafted, Finalize) 갱신
    """
    msg, user = state["message"], state.get("user_id") or ""
    extracted, usage = None, None
    try:
        extracted, usage = await deps.fast_llm.json(EXTRACT_SYS, msg, SCHEDULE_ADD_SCHEMA)
    # 추출 실패는 초안을 막지 않는다 — 제목은 코드가 원문에서 만든다
    except Exception:  # pylint: disable=broad-exception-caught
        extracted = None
    draft = build_draft(msg, deps.today, user, extracted, state.get("scope_project") or "")
    conflicts = []
    if draft["date"]:
        events = deps.calendar.list_events(draft["date"], draft["date"]) if deps.calendar else []
        meetings = [m for m in await deps.mcp.call("list_meetings") if m["date"] == draft["date"]]
        conflicts = find_conflicts(draft, events, meetings)
    perm, why = add_permission(user, draft["attendees"])
    draft |= {"conflicts": conflicts, "permission": perm, "permission_note": why}
    u = dict(state.get("usage") or {"llm_calls": 0, "tokens": 0})
    if usage:
        u = {"llm_calls": u["llm_calls"] + 1, "tokens": u["tokens"] + usage.get("tokens", 0)}
    return {"schedule_draft": draft, "final": render_draft(draft), "usage": u,
            "events": state.get("events", []) + [
                {"type": "ScheduleDrafted", "date": draft["date"], "start": draft["start"],
                 "missing": draft["missing"], "conflicts": len(conflicts), "title_source": draft["title_source"]},
                {"type": "Finalize", "pipeline": "schedule_add"}]}
