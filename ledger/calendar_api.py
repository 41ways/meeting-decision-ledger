"""일정 API (FastAPI 라우터) — 일정 저장은 이 확인 API에서만 한다.

- GET  /events          : 일정 목록 (달력 드로어·미니 달력)
- POST /events/preview  : 화면에서 고친 초안을 다시 검사 (형식·참석자·겹침·권한). 저장하지 않음
- POST /events          : 사람이 '추가'를 누른 초안 저장. 인턴이 다른 사람을 넣은 일정은 저장 대신 팀장 요청 알림

server.py가 include_router로 붙인다. 저장소는 app.state.deps.calendar(CalendarProvider)만 본다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ledger.calendar import find_conflicts
from ledger.schedule_parse import add_permission, kind_of, match_user, validate_draft

router = APIRouter()
KEEP = ("date", "start", "end", "title", "attendees", "project")


class EventReq(BaseModel):
    """
    일정 확인·저장 요청 본문
    Attributes:
        user: 요청자 ID
        draft: 일정 초안 (date, start, end, title, attendees, project). 화면에서 고친 값일 수 있다
    """
    user: str
    draft: dict


def _clean(draft: dict, user: str) -> dict:
    """
    초안에서 저장할 필드만 남기고 다듬기 (참석자 이름 정규화, 종류는 제목으로 다시 계산)
    Args:
        draft: 일정 초안
        user: 요청자 ID
    Returns:
        저장용 일정 dict
    """
    ev = {k: draft.get(k) or ("" if k != "attendees" else []) for k in KEEP}
    if isinstance(ev["attendees"], str):
        ev["attendees"] = [x for x in (s.strip() for s in ev["attendees"].split(",")) if x]
    ev["attendees"] = [(match_user(n) or {"name": n})["name"] for n in ev["attendees"]]
    ev["title"] = ev["title"].strip()
    return ev | {"kind": kind_of(ev["title"]), "created_by": user}


async def _check(request: Request, ev: dict) -> dict:
    """
    저장 전 검사 — 형식 오류, 같은 참석자의 겹치는 일정·회의, 권한
    Args:
        request: FastAPI 요청 (app.state의 deps 사용)
        ev: 다듬은 일정
    Returns:
        errors, conflicts, permission, permission_note
    """
    deps = request.app.state.deps
    errors = validate_draft(ev)
    conflicts = []
    if not errors:
        meetings = [m for m in await deps.mcp.call("list_meetings") if m["date"] == ev["date"]]
        conflicts = find_conflicts(ev, deps.calendar.list_events(ev["date"], ev["date"]), meetings)
    perm, why = add_permission(ev["created_by"], ev["attendees"])
    return {"errors": errors, "conflicts": conflicts, "permission": perm, "permission_note": why}


@router.get("/events")
async def list_events(request: Request, date_from: str = "", date_to: str = ""):
    """
    일정 목록 조회 (팀 달력이라 모두에게 보인다. 회의 원문 권한과는 별개)
    Args:
        request: FastAPI 요청
        date_from: 시작일 (비우면 처음부터)
        date_to: 종료일 (비우면 끝까지)
    Returns:
        events(일정 목록), provider(local|google)
    """
    cal = request.app.state.deps.calendar
    return {"events": cal.list_events(date_from, date_to), "provider": cal.name}


@router.post("/events/preview")
async def preview_event(request: Request, req: EventReq):
    """
    화면에서 고친 초안 다시 검사 — 저장하지 않는다
    Args:
        request: FastAPI 요청
        req: 요청자·초안
    Returns:
        다듬은 일정(event)과 errors, conflicts, permission, permission_note
    """
    ev = _clean(req.draft, req.user)
    return {"event": ev, **await _check(request, ev)}


@router.post("/events")
async def create_event(request: Request, req: EventReq):
    """
    사람이 '추가'를 누른 일정 저장

    형식 오류가 있으면 저장하지 않는다. 겹침은 막지 않는다 (화면에서 확인 대화상자로 이미 물어봄).
    인턴이 다른 사람이 들어간 일정을 넣으면 저장하지 않고 팀장에게 요청 알림을 보낸다.
    저장되면 요청자를 뺀 참석자에게 알림이 간다.
    Args:
        request: FastAPI 요청
        req: 요청자·초안
    Returns:
        저장된 일정(event)과 dry_run 여부, 또는 requested=True
    Raises:
        HTTPException: 400 — 형식 오류 / 403 — 등록되지 않은 사용자
    """
    acc, cal = request.app.state.access, request.app.state.deps.calendar
    ev = _clean(req.draft, req.user)
    chk = await _check(request, ev)
    if chk["errors"]:
        raise HTTPException(400, " ".join(chk["errors"]))
    if chk["permission"] == "deny":
        raise HTTPException(403, chk["permission_note"])
    me = acc.user(req.user)["name"]
    when = f"{ev['date']} {ev['start']}" + (f"~{ev['end']}" if ev["end"] else "")
    if chk["permission"] == "request":
        for lead in acc.leads():
            acc.notify(lead, f"{me} 일정 추가 요청: {when} {ev['title']} (참석 {', '.join(ev['attendees'])})", "request")
        return {"requested": True, "event": ev}
    saved = cal.create_event(ev)
    if not saved.get("dry_run"):
        names = {u["name"]: uid for uid, u in acc.users.items()}
        for n in ev["attendees"]:
            if n != me and n in names:
                acc.notify(names[n], f"{me} 님이 일정을 추가했습니다: {when} {ev['title']}", "info")
    return {"event": saved, "dry_run": bool(saved.get("dry_run")), "conflicts": chk["conflicts"]}
