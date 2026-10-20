"""결정 원장 Agent — FastAPI 백엔드 (베이스 server.py 구조 유지).

- Wave 1  : lifespan에서 MCP 연결·LLM·그래프를 프로세스당 1회 생성, app.state로 공유. Pydantic 요청 검증
- Wave 2  : mcp_ledger_server.py를 stdio 자식 프로세스로 띄워 연결 유지
- Wave 3  : GET /chat/stream — 그래프 진행 상황과 답변 토큰을 SSE로 스트리밍
- Wave 12 : 대화 이력은 SQLite(session.db), 대화방·권한·알림은 store/access.db
- HITL    : 결정·의견 승인과 열람 승인은 팀장만 (관리자 콘솔). 그래프는 '승인 대기' 등록까지만
- 권한    : 회의 원문·결정은 기본적으로 그 회의 참석자만. 권한 없는 결정은 그래프가 내용을 LLM에 넘기지 않는다

실행: uvicorn server:app --port 8100  →  브라우저 http://127.0.0.1:8100
"""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()   # LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY — import보다 먼저

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from langsmith import traceable
from pydantic import BaseModel

from agent.ledger_graph import LedgerDeps, build_ledger_graph, normalize_meeting_id
from agent.session_store import SqliteSessionStore
from ledger.access import AccessStore
from ledger.llm import OLLAMA_URL, LocalLLM, build_llms, warm_up
from dataclasses import replace
from ledger.mcp_bridge import LedgerMCP

HERE = Path(__file__).parent
VERSION = "0.7.0"
# 모델 조합 (Wave 10a 티어링). 입력창 아래에서 질문마다 고른다. 조합마다 그래프를 미리 만들어 사람마다 달라도 서로 영향 없음
PRESETS = {"accurate": ("정확", "qwen3:8b", "qwen3:8b"),
           "fast": ("빠름", "qwen3:0.6b", "qwen3:8b"),
           "fastest": ("가장 빠름", "qwen3:0.6b", "qwen3:0.6b")}
BUILD = "2026-10-20"


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"
    user: str = "kjh"


class UserReq(BaseModel):
    user: str


class RejectReq(BaseModel):
    user: str
    reason: str = ""


class ProposalReq(BaseModel):
    user: str
    statement: str
    topic: str
    project_id: str = "PRJ-OPSAI"
    source: str = "의견 검토"


class AccessReq(BaseModel):
    user: str
    target: str
    reason: str = ""


class DecideReq(BaseModel):
    user: str
    approve: bool
    scope: str = "user"          # user | role | all
    duration: str = "once"       # once | project | forever | until
    until: str = ""              # duration=until일 때 기한 (YYYY-MM-DD)


class AclReq(BaseModel):
    user: str
    meeting_id: str
    role: str
    on: bool


async def refresh_meetings() -> dict:
    rows = await app.state.deps.mcp.call("list_meetings")
    app.state.meetings = {m["meeting_id"]: m for m in rows}
    return app.state.meetings


def can_view_decision(user_id: str, decision: dict, consume: bool = False) -> bool:
    return app.state.access.can_view_decision(user_id, decision, app.state.meetings, consume)


def can_view_decision_for_answer(user_id: str, decision: dict) -> bool:
    """그래프가 답변 근거로 실제로 쓰는 순간 — 1회 열람권은 여기서 쓴다."""
    return can_view_decision(user_id, decision, consume=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp = LedgerMCP()
    await mcp.start()
    fast, quality = build_llms()
    app.state.access = AccessStore()
    deps = LedgerDeps(fast_llm=fast, quality_llm=quality, mcp=mcp, session_store=SqliteSessionStore(),
                      propose_to_ledger=True, can_view=can_view_decision_for_answer)
    app.state.deps = deps
    app.state.graph = build_ledger_graph(deps)
    app.state.graphs = {"accurate": app.state.graph}
    if os.environ.get("LLM_BACKEND", "ollama") == "ollama":
        for key, (_, f, q) in PRESETS.items():
            if key != "accurate":
                app.state.graphs[key] = build_ledger_graph(replace(deps, fast_llm=LocalLLM(f, 0.00015, "fast"),
                                                                    quality_llm=LocalLLM(q, 0.005, "quality")))
        asyncio.create_task(warm_up(["qwen3:8b", "qwen3:0.6b"]))     # 첫 질문 전에 모델을 메모리에 올려 둔다
    await refresh_meetings()
    yield
    await mcp.stop()
    deps.session_store.close()
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


def lead_only(user: str) -> None:
    if not app.state.access.is_lead(user):
        raise HTTPException(403, "팀장만 할 수 있습니다.")


@app.get("/")
async def index():
    return FileResponse(HERE / "web_chat.html")


@app.get("/health")
async def health():
    d = app.state.deps
    return {"status": "ok", "version": VERSION, "build": BUILD, "fast_llm": d.fast_llm.name,
            "quality_llm": d.quality_llm.name, "mcp_calls": d.mcp.calls}


# ---------------------------------------------------------------- 사용자·대화방
@app.get("/users")
async def users():
    return {"users": list(app.state.access.users.values())}


@app.get("/threads")
async def threads(user: str):
    return {"threads": app.state.access.threads(user)}


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, user: str):
    if not app.state.access.delete_thread(thread_id, user):
        raise HTTPException(404, "본인 대화방만 지울 수 있습니다.")
    app.state.deps.session_store.delete(thread_id)
    return {"deleted": thread_id}


@app.get("/threads/{thread_id}/messages")
async def thread_messages(thread_id: str):
    return {"messages": app.state.deps.session_store.load(thread_id)}


# ---------------------------------------------------------------- 채팅
def precheck(user: str, message: str, scope_meeting: str = "") -> str | None:
    """회의·면접 정리 요청이면 열람 권한부터 본다. 막히면 안내문을 돌려준다."""
    mid = normalize_meeting_id(message) or scope_meeting
    if not mid or not re.search(r"정리|요약|추출|처리|분석", message):
        return None
    m = app.state.meetings.get(mid)
    if m and not app.state.access.can_view_meeting(user, m, consume=True):   # 실제로 여는 순간이라 1회 열람권 사용
        return mid
    return None


SUMMARY_WORDS = re.compile(r"(?:[A-Z]{3}-)?[MI]\d{2}|회의|면접|정리|요약|추출|해\s*주세요|해\s*줘|부탁(?:해요|드려요)?|좀|해요|[\s.,!?~]")
FRESH = re.compile(r"다시|새로|재생성|최신")


def cache_key(user: str, message: str, project: str = "", meeting: str = "") -> str | None:
    """같은 요청이면 같은 키. 회의·면접 정리는 대상 회의로(사람 무관), 질문·의견은 사용자별 문장으로.
    정리 요청에 대상 말고 다른 말이 붙어 있으면(추가 요청) 키를 만들지 않아 새로 처리한다."""
    if FRESH.search(message):
        return None
    mid = normalize_meeting_id(message) or meeting
    if mid and re.search(r"정리|요약|추출", message):
        return None if SUMMARY_WORDS.sub("", message) else f"summary:{mid}"
    norm = re.sub(r"[\s.,!?~]", "", message)
    return f"ask:{user}:{project or 'auto'}:{meeting or '-'}:{norm}" if norm else None   # 범위가 다르면 다른 요청


def project_key_of(result: dict) -> str | None:
    mid = result.get("meeting_id") or ""
    if "-" in mid:
        return mid.split("-")[0]
    items = (result.get("report") or {}).get("items") or result.get("items") or []
    if items and items[0].get("project"):
        return app.state.access.key_of(items[0]["project"])
    return "OPS" if result.get("mode") in ("opinion", "question") else None


def after_run(user: str, thread_id: str, message: str, result: dict) -> None:
    acc = app.state.access
    report = result.get("report") or {}
    for did in report.get("proposed") or []:
        acc.record_proposal(did, user, result.get("meeting_id") or "회의 정리")
    acc.touch_thread(thread_id, user, message, project_key_of(result), result.get("meeting_id") or None)


@traceable(name="chat_request")
async def _run(message: str, thread_id: str, user: str) -> dict:
    deps = app.state.deps
    result = await app.state.graph.ainvoke({"message": message, "user_id": user,
                                            "history": deps.session_store.load(thread_id), "events": []})
    deps.session_store.append(thread_id, "user", message)
    deps.session_store.append(thread_id, "assistant", result["final"])
    after_run(user, thread_id, message, result)
    return result


@app.post("/chat")
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(400, "입력이 비어 있습니다.")
    denied = precheck(req.user, req.message)
    if denied:
        return {"reply": f"{denied} 열람 권한이 없습니다. 열람 요청을 보내면 팀장이 승인합니다.",
                "denied": [denied], "events": []}
    r = await _run(req.message, req.thread_id, req.user)
    return {"reply": r["final"], "mode": r.get("mode"), "report": r.get("report"),
            "events": r.get("events", []), "usage": r.get("usage")}


@app.get("/chat/stream")
async def chat_stream(message: str, thread_id: str = "default", user: str = "kjh", force: bool = False,
                      project: str = "", meeting: str = "", model: str = "accurate"):
    """노드가 끝날 때마다 새 이벤트를, 답변 작성 중에는 토큰을 'data: {json}\\n\\n'으로 보낸다."""
    deps = app.state.deps

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    async def gen():
        if not message.strip():
            yield sse({"type": "Error", "message": "입력이 비어 있습니다."})
            yield "data: [DONE]\n\n"
            return
        denied = precheck(user, message, meeting)
        if denied:
            yield sse({"type": "AccessDenied", "targets": [denied]})
            yield sse({"type": "FinalAnswer", "reply": f"{denied} 열람 권한이 없습니다. 아래에서 열람 요청을 보내면 팀장이 승인합니다."})
            yield "data: [DONE]\n\n"
            return
        # 대화방과 질문은 시작할 때 바로 저장 — 답변 도중 사용자를 바꾸거나 창을 닫아도 대화방이 남는다
        mid = (normalize_meeting_id(message) or meeting) if re.search(r"정리|요약|추출|처리|분석", message) else ""
        # 대화방에 범위를 기록: 회의가 있으면 그 회의·프로젝트, 아니면 사용자가 고른 프로젝트·회의
        app.state.access.touch_thread(thread_id, user, message, (mid.split("-")[0] if mid else None) or project or None,
                                      mid or meeting or None)
        history = deps.session_store.load(thread_id)
        deps.session_store.append(thread_id, "user", message)

        # 같은 요청 재사용: 원장·정책·문서·원문이 그대로면 저장된 답을 돌려준다
        graph = app.state.graphs.get(model) or app.state.graph
        key = None if force else cache_key(user, message, project, meeting)
        key = key and f"{key}|{model}"                       # 모델이 다르면 다른 답
        fp = await deps.mcp.call("ledger_fingerprint")
        hit = app.state.access.cache_get(key, fp) if key else None
        if hit:
            yield sse({"type": "CacheHit", "created": hit["created"]})
            for ev in hit.get("events", []):
                yield sse(ev)
            if hit.get("restricted"):
                yield sse({"type": "AccessDenied", "targets": hit["restricted"]})
            yield sse({"type": "FinalAnswer", "reply": hit["final"], "mode": hit.get("mode"), "cached": True,
                       "opinion": hit.get("opinion")})
            deps.session_store.append(thread_id, "assistant", hit["final"])
            yield "data: [DONE]\n\n"
            return
        seen, state = 0, {}
        try:
            async for mode, payload in graph.astream(
                    {"message": message, "user_id": user, "history": history, "events": [],
                     "scope_project": project, "scope_meeting": meeting},
                    stream_mode=["updates", "custom"]):
                if mode == "custom":
                    yield sse(payload)
                    continue
                for node_name, delta in payload.items():
                    evs = delta.get("events") or []
                    for ev in evs[seen:]:
                        yield sse({**ev, "node": node_name})
                    if evs:
                        seen = len(evs)
                    state.update({k: v for k, v in delta.items() if k != "events"})
        except Exception as e:     # LLM 서버 다운·MCP 오류 등 — 화면에 원인을 보여 주고 스트림을 정상 종료
            hint = "LLM 서버(Ollama)에 연결할 수 없습니다. ollama serve 확인" if "Connect" in type(e).__name__ else repr(e)
            deps.session_store.append(thread_id, "assistant", "오류: " + hint)
            yield sse({"type": "Error", "message": hint})
            yield "data: [DONE]\n\n"
            return
        report = state.get("report") or {}
        if report.get("restricted"):
            yield sse({"type": "AccessDenied", "targets": report["restricted"]})
        final = state.get("final", "")
        opinion = ({"statement": message, "topic": report["items"][0]["topic"], "status": report["items"][0]["review"]["status"]}
                   if state.get("mode") == "opinion" and report.get("items") else None)
        yield sse({"type": "FinalAnswer", "reply": final, "mode": state.get("mode"),
                   "proposed": report.get("proposed", []), "opinion": opinion})
        if key and state.get("mode") in ("meeting", "interview", "opinion", "question"):
            # 지문은 처리 후 값으로 저장 (이번 처리에서 올린 승인 대기 결정까지 반영된 상태)
            app.state.access.cache_put(key, await deps.mcp.call("ledger_fingerprint"), final,
                                       {"mode": state.get("mode"), "opinion": opinion, "restricted": report.get("restricted", []),
                                        "events": [e for e in state.get("events", []) if e.get("type") not in ("Finalize",)]})
        deps.session_store.append(thread_id, "assistant", final)
        after_run(user, thread_id, message, state)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- 캘린더·프로젝트·원장
@app.get("/meetings")
async def meetings(user: str):
    ms = await refresh_meetings()
    return {"meetings": [{**m, "can_view": app.state.access.can_view_meeting(user, m)} for m in ms.values()]}


@app.get("/projects")
async def projects():
    rows = await app.state.deps.mcp.call("list_decisions", project_id="")
    out = []
    for p in app.state.access.projects:
        mine = [r for r in rows if r["project_id"] == p["project_id"]]
        out.append({**p, "decisions": len(mine), "pending": sum(r["status"] == "proposed" for r in mine),
                    "meetings": sum(m["project_id"] == p["project_id"] for m in app.state.meetings.values())})
    return {"projects": out}


@app.get("/projects/{key}/ledger")
async def project_ledger(key: str, user: str):
    p = next((x for x in app.state.access.projects if x["key"] == key), None)
    if p is None:
        raise HTTPException(404, key)
    rows = await app.state.deps.mcp.call("list_decisions", project_id=p["project_id"])
    out = []
    for r in rows:
        ok = can_view_decision(user, r)
        out.append({k: v for k, v in r.items() if k != "evidence"} | {
            "can_view": ok, "statement": r["statement"] if ok else "열람 권한 없음",
            "meetings": sorted({e["meeting_id"] for e in r.get("evidence", [])})})
    return {"project": p, "decisions": out}


# ---------------------------------------------------------------- 승인 (팀장)
@app.post("/proposals")
async def propose(req: ProposalReq):
    """의견 검토 결과를 결정으로 올려 달라는 승인 요청."""
    name = app.state.access.user(req.user)["name"]
    r = await app.state.deps.mcp.call("propose_decision", statement=req.statement, topic=req.topic,
                                      decided_by=name, evidence=[], project_id=req.project_id)
    app.state.access.record_proposal(r["proposed"], req.user, req.source)
    return r


@app.get("/admin/pending")
async def admin_pending(user: str):
    lead_only(user)
    acc = app.state.access
    rows = await app.state.deps.mcp.call("list_decisions", project_id="")
    src = acc.proposal_sources()
    pending = [{k: v for k, v in r.items() if k != "evidence"} | {
        "requested_by": acc.user(src[r["decision_id"]]["requested_by"])["name"] if r["decision_id"] in src else r.get("decided_by"),
        "source": src.get(r["decision_id"], {}).get("source", "원장"),
        "meetings": sorted({e["meeting_id"] for e in r.get("evidence", [])})} for r in rows if r["status"] == "proposed"]
    return {"decisions": pending, "requests": acc.requests("pending"), "history": acc.requests(None)[:20]}


@app.post("/decisions/{decision_id}/approve")
async def approve(decision_id: str, req: UserReq):
    lead_only(req.user)
    acc = app.state.access
    r = await app.state.deps.mcp.call("approve_decision", decision_id=decision_id,
                                      approved_by=f"{acc.user(req.user)['name']} {acc.user(req.user)['role']}")
    if isinstance(r, dict) and r.get("error"):
        raise HTTPException(400, r["error"])
    acc.after_decision(decision_id, req.user, True, r.get("superseded", []))
    return r


@app.post("/decisions/{decision_id}/reject")
async def reject(decision_id: str, req: RejectReq):
    lead_only(req.user)
    acc = app.state.access
    r = await app.state.deps.mcp.call("reject_decision", decision_id=decision_id,
                                      rejected_by=acc.user(req.user)["name"], reason=req.reason)
    acc.after_decision(decision_id, req.user, False, [], req.reason)
    return r


@app.post("/access-requests")
async def access_request(req: AccessReq):
    return {"id": app.state.access.request_access(req.user, req.target, req.reason)}


@app.post("/access-requests/{req_id}/decide")
async def access_decide(req_id: int, req: DecideReq):
    lead_only(req.user)
    return app.state.access.decide_request(req_id, req.user, req.approve, req.scope, req.duration, req.until)


@app.get("/admin/acl")
async def admin_acl(user: str):
    lead_only(user)
    acl = app.state.access.acl()
    return {"meetings": [{**m, "roles": acl.get(m["meeting_id"], [])} for m in app.state.meetings.values()]}


@app.post("/admin/acl")
async def admin_acl_set(req: AclReq):
    lead_only(req.user)
    app.state.access.set_acl(req.meeting_id, req.role, req.on, req.user)
    return {"ok": True}


# ---------------------------------------------------------------- 알림·RAG
# ---------------------------------------------------------------- 모델 조합 목록 (입력창 아래 선택용)
@app.get("/models")
async def models():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            installed = {m["name"] for m in (await c.get(f"{OLLAMA_URL}/api/tags")).json().get("models", [])}
    except Exception:
        installed = set()
    out = [{"key": k, "label": lab, "fast": f, "quality": q, "available": k in app.state.graphs and {f, q} <= installed}
           for k, (lab, f, q) in PRESETS.items()]
    return {"presets": out, "default": "accurate"}


@app.get("/notifications")
async def notifications(user: str):
    rows = app.state.access.notifications(user)
    return {"notifications": rows, "unread": sum(not r["read"] for r in rows)}


@app.post("/notifications/read")
async def notifications_read(req: UserReq):
    app.state.access.mark_read(req.user)
    return {"ok": True}


@app.get("/rag/counts")
async def rag_counts():
    return await app.state.deps.mcp.call("rag_counts")


@app.get("/rag/search")
async def rag_search(collection: str, q: str, filtered: bool = True, k: int = 6):
    return {"rows": await app.state.deps.mcp.call("rag_inspect", collection=collection, query=q, filtered=filtered, k=k)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
