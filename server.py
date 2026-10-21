"""결정 원장 Agent — FastAPI 백엔드 (베이스 server.py 구조 유지).

- Wave 1  : lifespan에서 MCP 연결·LLM·그래프를 프로세스당 1회 생성, app.state로 공유. Pydantic 요청 검증
- Wave 2  : mcp_ledger_server.py를 stdio 자식 프로세스로 띄워 연결 유지
- Wave 3  : GET /chat/stream — 그래프 진행 상황과 답변 토큰을 SSE로 스트리밍
- Wave 12 : 대화 이력은 SQLite(session.db), 대화방·권한·알림은 store/access.db
- HITL    : 결정·의견 승인과 열람 승인은 팀장만 (관리자 콘솔). 그래프는 '승인 대기' 등록까지만
- 권한    : 회의 원문·결정은 기본적으로 그 회의 참석자만. 권한 없는 결정은 그래프가 내용을 LLM에 넘기지 않는다

실행: uvicorn server:app --port 8100  →  브라우저 http://127.0.0.1:8100
"""
# pylint: disable=too-many-lines  # 화면이 부르는 엔드포인트를 한곳에서 찾게 둔다 (새 묶음은 ledger/*_api.py 라우터로)
import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()   # LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY — langsmith import 전에 환경변수가 있어야 추적이 켜짐

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from langsmith import traceable
from pydantic import BaseModel

from agent.enrich import _overlap
from agent.crosscheck import build_checkers
from agent.ledger_graph import TODAY, LedgerDeps, build_ledger_graph, normalize_meeting_id
from agent.session_store import SqliteSessionStore
from ledger.access import AccessStore
from ledger.calendar import build_calendar
from ledger.calendar_api import router as calendar_router
from ledger.llm import OLLAMA_URL, LocalLLM, build_llms, warm_up
from ledger.mcp_bridge import LedgerMCP
from ledger import monitor_api

HERE = Path(__file__).parent
VERSION = "0.7.0"
# 모델 조합 (Wave 10a 티어링). 값은 (화면 이름, 분류용 fast 모델, 작성용 quality 모델)
# 입력창 아래에서 질문마다 고른다. 조합마다 그래프를 미리 만들어 두어 사람마다 달라도 서로 영향 없음
PRESETS = {"auto": ("자동", "qwen3:8b", "qwen3:8b"),          # 회의 정리·질의·의견은 8b, 일반 대화는 0.6b, 일정은 모델 없이
           "accurate": ("정확", "qwen3:8b", "qwen3:8b"),
           "fast": ("빠름", "qwen3:0.6b", "qwen3:8b"),
           "fastest": ("가장 빠름", "qwen3:0.6b", "qwen3:0.6b")}
BUILD = "2026-10-20"


class ChatRequest(BaseModel):
    """
    POST /chat 요청 본문
    Attributes:
        message: 사용자 입력 문장
        thread_id: 대화방 ID (대화 이력 저장 키)
        user: 사용자 ID (권한 판정 기준)
    """
    message: str
    thread_id: str = "default"
    user: str = "kjh"


class UserReq(BaseModel):
    """
    사용자 ID만 담는 요청 본문 (승인·알림 읽음 처리)
    Attributes:
        user: 요청한 사용자 ID
    """
    user: str


class EditReq(BaseModel):
    """
    승인 전 결정 수정 요청 본문
    Attributes:
        user:         요청한 사용자 ID (팀장)
        statement:    고친 결정 문장 (비우면 그대로)
        topic:        고친 주제 키 (비우면 그대로)
        rationale:    고친 이유 (None이면 그대로)
        alternatives: 고친 기각 대안 목록 (None이면 그대로)
        slots:        고친 칸 기록 {target, change, scope, condition} (None이면 그대로)
    """
    user: str
    statement: str = ""
    topic: str = ""
    rationale: str | None = None
    alternatives: list[str] | None = None
    slots: dict | None = None


class CloseReq(BaseModel):
    """
    미결 닫기 요청 본문
    Attributes:
        user:    요청한 사용자 ID (팀장)
        item_id: 미결 항목 ID
        reason:  닫는 사유
    """
    user: str
    item_id: str
    reason: str = ""


class RejectReq(BaseModel):
    """
    결정 반려 요청 본문
    Attributes:
        user: 반려하는 사용자 ID (팀장)
        reason: 반려 사유. 제안자 알림에 함께 표시
    """
    user: str
    reason: str = ""


class ProposalReq(BaseModel):
    """
    의견 검토 결과를 결정 승인 대기로 올리는 요청 본문
    Attributes:
        user: 제안자 사용자 ID
        statement: 결정 문장
        topic: 결정 주제 (원장에서 같은 주제의 유효 결정과 대체 관계를 판단하는 기준)
        project_id: 대상 프로젝트 ID
        source: 요청 출처 (관리자 콘솔에 표시)
    """
    user: str
    statement: str
    topic: str
    project_id: str = "PRJ-OPSAI"
    source: str = "의견 검토"


class AccessReq(BaseModel):
    """
    열람 요청 본문
    Attributes:
        user: 요청자 사용자 ID
        target: 열람 대상 (회의 ID 또는 결정 ID)
        reason: 요청 사유
    """
    user: str
    target: str
    reason: str = ""


class DecideReq(BaseModel):
    """
    열람 요청 승인·반려 본문
    Attributes:
        user: 처리하는 사용자 ID (팀장)
        approve: 승인 여부
        scope: 허용 범위 — user(요청자만) | role(같은 역할 전체) | all(전체)
        duration: 허용 기간 — once(1회) | project(프로젝트 진행 중) | forever(무기한) | until(지정일까지)
        until: duration=until일 때 기한 (YYYY-MM-DD)
    """
    user: str
    approve: bool
    scope: str = "user"          # user | role | all
    duration: str = "once"       # once | project | forever | until
    until: str = ""              # duration=until일 때 기한 (YYYY-MM-DD)


class AclReq(BaseModel):
    """
    회의별 역할 접근 권한 변경 본문
    Attributes:
        user: 변경하는 사용자 ID (팀장)
        meeting_id: 대상 회의 ID
        role: 대상 역할
        on: True면 열람 허용, False면 해제
    """
    user: str
    meeting_id: str
    role: str
    on: bool


async def refresh_meetings() -> dict:
    """
    MCP에서 회의 목록을 다시 읽어 app.state.meetings 캐시를 갱신

    권한 판정(can_view_decision·precheck)이 이 캐시의 참석자 정보를 쓰므로 회의 목록 조회 때마다 새로 맞춘다.
    Returns:
        회의 ID → 회의 정보 사전
    """
    rows = await app.state.deps.mcp.call("list_meetings")
    app.state.meetings = {m["meeting_id"]: m for m in rows}
    return app.state.meetings


def can_view_decision(user_id: str, decision: dict, consume: bool = False) -> bool:
    """
    사용자가 결정을 열람할 수 있는지 판정 (AccessStore 위임)
    Args:
        user_id: 사용자 ID
        decision: 원장 결정 레코드 (evidence의 회의 ID로 참석 여부를 본다)
        consume: True면 1회 열람권을 사용 처리
    Returns:
        열람 가능 여부
    """
    return app.state.access.can_view_decision(user_id, decision, app.state.meetings, consume)


def can_view_decision_for_answer(user_id: str, decision: dict) -> bool:
    """
    그래프가 답변 근거로 실제로 쓰는 순간의 열람 판정 — 1회 열람권은 여기서 쓴다

    LedgerDeps.can_view로 주입. 목록 표시(can_view_decision 기본값)는 열람권을 쓰지 않는다.
    Args:
        user_id: 사용자 ID
        decision: 원장 결정 레코드
    Returns:
        열람 가능 여부
    """
    return can_view_decision(user_id, decision, consume=True)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    서버 시작·종료 시 공유 자원 생성과 정리 (Wave 1·2)

    MCP 자식 프로세스, LLM, 권한 저장소, 그래프를 프로세스당 1회만 만들어 application.state에 둔다.
    Ollama 백엔드면 모델 조합(PRESETS)별 그래프를 미리 만들고 모델을 예열한다.
    Args:
        application: FastAPI 앱
    Returns:
        종료 시점까지 대기하는 비동기 컨텍스트 (yield 이후 정리)
    """
    mcp = LedgerMCP()
    await mcp.start()
    fast, quality = build_llms()
    application.state.access = AccessStore()
    deps = LedgerDeps(fast_llm=fast, quality_llm=quality, mcp=mcp, session_store=SqliteSessionStore(),
                      checkers=build_checkers(),   # CROSSCHECK=1이면 작은 모델 교차검증
                      propose_to_ledger=True, can_view=can_view_decision_for_answer, calendar=build_calendar())
    application.state.deps = deps
    application.state.graph = build_ledger_graph(deps)
    # 기본 그래프(build_llms 기준)를 'accurate'로 등록. OpenAI 백엔드면 이 하나만 쓴다
    application.state.graphs = {"accurate": application.state.graph}
    # 파이프라인 버전 등록·요청 지표 저장소 (승격은 scripts/promote.py, 감시는 GET /admin/monitor)
    monitor_api.setup(application.state, fast.name, quality.name,
                      PRESETS if os.environ.get("LLM_BACKEND", "ollama") == "ollama" else {})
    if os.environ.get("LLM_BACKEND", "ollama") == "ollama":
        for key, (_, f, q) in PRESETS.items():
            if key != "accurate":
                # deps를 복사해 LLM만 바꾼다 — MCP·세션 저장소·권한 판정은 공유. 'auto'만 일반 대화를 0.6b로
                application.state.graphs[key] = build_ledger_graph(replace(
                    deps, fast_llm=LocalLLM(f, 0.00015, "fast"), quality_llm=LocalLLM(q, 0.005, "quality"),
                    chat_llm=LocalLLM("qwen3:0.6b", 0.00015, "chat") if key == "auto" else None))
        asyncio.create_task(warm_up(["qwen3:8b", "qwen3:0.6b"]))     # 첫 질문 전에 모델을 메모리에 올려 둔다
    await refresh_meetings()
    yield
    await mcp.stop()
    deps.session_store.close()
    # 종료 직전 LangSmith 추적을 마저 전송. 추적이 꺼져 있거나 패키지가 없어도 종료는 막지 않는다
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers  # pylint: disable=import-outside-toplevel
        wait_for_all_tracers()
    except Exception:  # pylint: disable=broad-exception-caught
        pass


app = FastAPI(lifespan=lifespan)
app.include_router(calendar_router)     # 일정 목록·확인·저장 (ledger/calendar_api.py)
app.include_router(monitor_api.router)  # 품질 감시 (ledger/monitor_api.py)


def lead_only(user: str) -> None:
    """
    팀장 전용 엔드포인트의 권한 확인 (HITL 승인은 팀장만)
    Args:
        user: 요청한 사용자 ID
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    if not app.state.access.is_lead(user):
        raise HTTPException(403, "팀장만 할 수 있습니다.")


@app.get("/")
async def index():
    """
    채팅 화면(web_chat.html) 제공
    Returns:
        HTML 파일 응답
    """
    return FileResponse(HERE / "web_chat.html")


@app.get("/health")
async def health():
    """
    서버 상태와 사용 중인 모델·MCP 호출 수 확인
    Returns:
        상태, 버전, 빌드, fast·quality 모델 이름, 누적 MCP 호출 수
    """
    d = app.state.deps
    return {"status": "ok", "version": VERSION, "build": BUILD, "fast_llm": d.fast_llm.name,
            "quality_llm": d.quality_llm.name, "mcp_calls": d.mcp.calls}


# ---------------------------------------------------------------- 사용자·대화방
@app.get("/users")
async def users():
    """
    사용자 목록 조회 (화면의 사용자 전환용)
    Returns:
        사용자 정보 목록
    """
    return {"users": list(app.state.access.users.values())}


@app.get("/threads")
async def threads(user: str):
    """
    사용자의 대화방 목록 조회 (최근 갱신 순)
    Args:
        user: 사용자 ID
    Returns:
        대화방 목록
    """
    return {"threads": app.state.access.threads(user)}


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, user: str):
    """
    대화방 삭제 — 본인 대화방만

    access.db의 대화방을 먼저 지우고, 성공했을 때만 session.db의 대화 내용도 지운다.
    Args:
        thread_id: 대화방 ID
        user: 요청한 사용자 ID
    Returns:
        삭제한 대화방 ID
    Raises:
        HTTPException: 404 — 없는 대화방이거나 본인 대화방이 아님
    """
    if not app.state.access.delete_thread(thread_id, user):
        raise HTTPException(404, "본인 대화방만 지울 수 있습니다.")
    app.state.deps.session_store.delete(thread_id)
    return {"deleted": thread_id}


@app.get("/threads/{thread_id}/messages")
async def thread_messages(thread_id: str):
    """
    대화방의 대화 이력 조회
    Args:
        thread_id: 대화방 ID
    Returns:
        role·content 메시지 목록 (저장 순서)
    """
    return {"messages": app.state.deps.session_store.load(thread_id)}


# ---------------------------------------------------------------- 채팅
def precheck(user: str, message: str, scope_meeting: str = "") -> str | None:
    """
    회의·면접 정리 요청이면 그래프 실행 전에 열람 권한부터 확인

    문장에 회의 ID가 없으면 화면에서 고른 회의(scope_meeting)를 대상으로 본다.
    Args:
        user: 사용자 ID
        message: 사용자 입력
        scope_meeting: 화면에서 고른 회의 ID
    Returns:
        권한이 없으면 막힌 회의 ID, 통과하거나 정리 요청이 아니면 None
    """
    mid = normalize_meeting_id(message) or scope_meeting
    # 정리류 동사가 있을 때만 원문을 여는 요청으로 본다. 질문·의견은 그래프 안에서 결정 단위로 판정
    if not mid or not re.search(r"정리|요약|추출|처리|분석", message):
        return None
    m = app.state.meetings.get(mid)
    # 목록에 없는 회의는 여기서 막지 않는다 (그래프가 '원문 없음'으로 처리)
    if m and not app.state.access.can_view_meeting(user, m, consume=True):   # 실제로 여는 순간이라 1회 열람권 사용
        return mid
    return None


# 정리 요청에서 '대상 회의 + 요청 표현'만 지우는 패턴. 지우고 남는 글자가 있으면 추가 요청이 붙은 것
SUMMARY_WORDS = re.compile(r"(?:[A-Z]{3}-)?[MI]\d{2}|회의|면접|정리|요약|추출|해\s*주세요|해\s*줘|부탁(?:해요|드려요)?|좀|해요|[\s.,!?~]")
# 사용자가 새 결과를 명시적으로 원하는 표현 — 저장된 답을 쓰지 않는다
FRESH = re.compile(r"다시|새로|재생성|최신")


def cache_key(user: str, message: str, project: str = "", meeting: str = "") -> str | None:
    """
    답변 재사용용 캐시 키 생성 — 같은 요청이면 같은 키

    회의·면접 정리는 대상 회의로 키를 만든다(사람 무관 — 열람 권한은 precheck에서 이미 확인).
    질문·의견은 사용자별 문장으로 만든다(결정별 열람 권한이 사람마다 달라 답이 다를 수 있음).
    정리 요청에 대상 말고 다른 말이 붙어 있으면(추가 요청) 키를 만들지 않아 새로 처리한다.
    Args:
        user: 사용자 ID
        message: 사용자 입력
        project: 화면에서 고른 프로젝트 범위
        meeting: 화면에서 고른 회의 범위
    Returns:
        'summary:{회의}' 또는 'ask:{사용자}:{프로젝트}:{회의}:{정규화 문장}'. 재사용하지 않을 요청이면 None
    Examples:
        cache_key("kjh", "M04 회의 정리해줘")          → "summary:OPS-M04"
        cache_key("kjh", "M04 정리하고 일정도 알려줘")  → None
        cache_key("kjh", "SSE는 왜 골랐어?")           → "ask:kjh:auto:-:SSE는왜골랐어"
    """
    if FRESH.search(message):
        return None
    mid = normalize_meeting_id(message) or meeting
    if mid and re.search(r"정리|요약|추출", message):
        return None if SUMMARY_WORDS.sub("", message) else f"summary:{mid}"
    norm = re.sub(r"[\s.,!?~]", "", message)        # 띄어쓰기·문장부호 차이는 같은 질문으로
    return f"ask:{user}:{project or 'auto'}:{meeting or '-'}:{norm}" if norm else None   # 범위가 다르면 다른 요청


def project_key_of(result: dict) -> str | None:
    """
    그래프 결과에서 대화방에 기록할 프로젝트 키 추출

    우선순위: 회의 ID 접두어(OPS-M04 → OPS) → 첫 결정 항목의 프로젝트 → 의견·질문이면 기본 OPS.
    Args:
        result: 그래프 최종 상태
    Returns:
        프로젝트 키. 판단할 근거가 없으면 None
    """
    mid = result.get("meeting_id") or ""
    if "-" in mid:
        return mid.split("-")[0]
    items = (result.get("report") or {}).get("items") or result.get("items") or []
    if items and items[0].get("project"):
        return app.state.access.key_of(items[0]["project"])
    return "OPS" if result.get("mode") in ("opinion", "question") else None


def after_run(user: str, thread_id: str, message: str, result: dict) -> None:
    """
    그래프 실행 뒤 승인 요청 기록과 대화방 갱신

    그래프가 원장에 올린 승인 대기 결정을 제안자와 함께 기록해 팀장에게 알림이 가게 한다.
    Args:
        user: 사용자 ID
        thread_id: 대화방 ID
        message: 사용자 입력 (새 대화방이면 제목으로 사용)
        result: 그래프 최종 상태
    """
    acc = app.state.access
    report = result.get("report") or {}
    for did in report.get("proposed") or []:
        acc.record_proposal(did, user, result.get("meeting_id") or "회의 정리")
    acc.touch_thread(thread_id, user, message, project_key_of(result), result.get("meeting_id") or None)


@traceable(name="chat_request")
async def _run(message: str, thread_id: str, user: str) -> dict:
    """
    비스트리밍 그래프 실행과 대화 이력 저장 (POST /chat용)

    LangSmith에 chat_request 단위로 추적된다. 이력은 실행 전 상태를 넘기고, 끝난 뒤 질문·답을 함께 저장한다.
    Args:
        message: 사용자 입력
        thread_id: 대화방 ID
        user: 사용자 ID
    Returns:
        그래프 최종 상태
    """
    deps, t0 = app.state.deps, time.perf_counter()
    result = await app.state.graph.ainvoke({"message": message, "user_id": user,
                                            "history": deps.session_store.load(thread_id), "events": []},
                                           config=monitor_api.run_config(app.state))
    monitor_api.record(app.state, result, t0, user)
    deps.session_store.append(thread_id, "user", message)
    deps.session_store.append(thread_id, "assistant", result["final"])
    after_run(user, thread_id, message, result)
    return result


@app.post("/chat")
async def chat(req: ChatRequest):
    """
    한 번에 답을 돌려주는 채팅 (스트리밍 없음)

    정리 요청이면 열람 권한을 먼저 확인하고, 막히면 그래프를 돌리지 않고 안내문만 돌려준다.
    Args:
        req: 메시지·대화방·사용자
    Returns:
        답변, 처리 모드, 보고서, 이벤트 목록, 사용량. 권한이 없으면 안내문과 막힌 회의 ID
    Raises:
        HTTPException: 400 — 빈 입력
    """
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
                      project: str = "", meeting: str = "", model: str = "auto"):
    """
    그래프 진행 상황과 답변 토큰을 SSE로 스트리밍하는 채팅 (Wave 3)

    노드가 끝날 때마다 새 이벤트를, 답변 작성 중에는 토큰을 'data: {json}\\n\\n'으로 보낸다.
    이벤트 순서:
        빈 입력      : Error → [DONE]
        권한 없음    : AccessDenied → FinalAnswer → [DONE]
        캐시 적중    : ModelInfo → CacheHit → 저장된 이벤트 → (AccessDenied) → FinalAnswer → [DONE]
        정상 실행    : ModelInfo → 노드 이벤트·토큰(custom) → (AccessDenied) → FinalAnswer → [DONE]
        실행 중 오류 : ModelInfo → ... → Error → [DONE]
    Args:
        message: 사용자 입력
        thread_id: 대화방 ID
        user: 사용자 ID
        force: True면 저장된 답을 쓰지 않고 새로 실행
        project: 화면에서 고른 프로젝트 범위
        meeting: 화면에서 고른 회의 범위
        model: 모델 조합 키 (PRESETS)
    Returns:
        text/event-stream 응답
    """
    deps = app.state.deps

    def sse(obj: dict) -> str:
        """
        SSE 한 건의 data 줄 생성
        Args:
            obj: 보낼 이벤트
        Returns:
            'data: {json}\\n\\n' 문자열 (한글은 이스케이프하지 않음)
        """
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    async def gen():
        """
        SSE 이벤트 생성기 — 권한 확인, 캐시 조회, 그래프 스트리밍, 결과 저장 순서로 진행

        어느 경로로 끝나든 마지막은 'data: [DONE]' (화면이 스트림 종료를 이것으로 판단).
        Returns:
            SSE 문자열을 차례로 내보내는 비동기 생성기
        """
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
        t0 = time.perf_counter()
        # 대화방과 질문은 시작할 때 바로 저장 — 답변 도중 사용자를 바꾸거나 창을 닫아도 대화방이 남는다
        mid = (normalize_meeting_id(message) or meeting) if re.search(r"정리|요약|추출|처리|분석", message) else ""
        # 대화방에 범위를 기록: 회의가 있으면 그 회의·프로젝트, 아니면 사용자가 고른 프로젝트·회의
        app.state.access.touch_thread(thread_id, user, message, (mid.split("-")[0] if mid else None) or project or None,
                                      mid or meeting or None)
        # 이력은 이번 질문을 저장하기 전 상태로 읽어 그래프에 넘긴다 (질문이 이력에 중복되지 않게)
        history = deps.session_store.load(thread_id)
        deps.session_store.append(thread_id, "user", message)
        refs = app.state.access.thread_refs(thread_id)     # 직전 답이 가리킨 결정·회의 ('이거' 풀이용)

        # 모르는 모델 키(또는 OpenAI 백엔드라 조합 그래프가 없음)면 기본 그래프·'정확' 표시로 대체
        graph = app.state.graphs.get(model) or app.state.graph
        lab, f, q = PRESETS.get(model, PRESETS["accurate"])
        yield sse({"type": "ModelInfo", "text": "모델 자동 — 회의 정리·질의·검토는 qwen3:8b, 일반 대화는 qwen3:0.6b, 일정은 모델 없이"
                   if model == "auto" else f"모델 {lab} — " + (q if f == q else f"분류 {f} · 작성 {q}")})
        # 같은 요청 재사용: 원장·정책·문서·원문이 그대로면(지문 일치) 저장된 답을 돌려준다
        key = None if force else cache_key(user, message, project, meeting)
        key = key and f"{key}|{model}"                       # 모델이 다르면 다른 답
        fp = await deps.mcp.call("ledger_fingerprint")
        hit = app.state.access.cache_get(key, fp) if key else None
        if hit:
            yield sse({"type": "CacheHit", "created": hit["created"]})
            # 저장해 둔 진행 이벤트를 다시 보내 화면의 진행 표시를 실제 실행과 같게 맞춘다
            for ev in hit.get("events", []):
                yield sse(ev)
            if hit.get("restricted"):
                yield sse({"type": "AccessDenied", "targets": hit["restricted"]})
            yield sse({"type": "FinalAnswer", "reply": hit["final"], "mode": hit.get("mode"), "cached": True,
                       "opinion": hit.get("opinion")})
            deps.session_store.append(thread_id, "assistant", hit["final"])
            app.state.access.set_thread_refs(thread_id, hit.get("refs") or [])
            monitor_api.record(app.state, {"mode": hit.get("mode"), "events": hit.get("events", []),
                                           "message": message}, t0, user, model, cached=True)
            yield "data: [DONE]\n\n"
            return
        # seen: 이미 보낸 이벤트 수. 노드는 누적된 events 목록 전체를 돌려주므로 새로 붙은 것만 보낸다
        # state: 노드 갱신분을 합친 최종 상태 (events는 위에서 따로 처리해 제외)
        seen, state = 0, {}
        tick, node_ms = time.perf_counter(), {}         # 노드별 지연 — 앞 노드가 끝난 뒤부터 이 노드가 끝날 때까지
        try:
            # updates = 노드 종료마다 상태 갱신분, custom = 노드가 직접 쓰는 답변 토큰 등
            async for mode, payload in graph.astream(
                    {"message": message, "user_id": user, "history": history, "events": [],
                     "scope_project": project, "scope_meeting": meeting, "context_refs": refs},
                    stream_mode=["updates", "custom"], config=monitor_api.run_config(app.state, model)):
                if mode == "custom":
                    yield sse(payload)
                    continue
                for node_name, delta in payload.items():
                    now = time.perf_counter()
                    node_ms[node_name] = node_ms.get(node_name, 0) + (now - tick) * 1000
                    tick = now
                    evs = delta.get("events") or []
                    for ev in evs[seen:]:
                        yield sse({**ev, "node": node_name})
                    if evs:
                        seen = len(evs)
                        state["events"] = evs        # 답변 재사용 때 처리 과정을 그대로 다시 보여 주려고 보관
                    state.update({k: v for k, v in delta.items() if k != "events"})
        # LLM 서버 다운·MCP 오류 등 — 화면에 원인을 보여 주고 스트림을 정상 종료
        except Exception as e:  # pylint: disable=broad-exception-caught
            # 연결 계열 예외(ConnectError 등)는 원인이 대부분 Ollama 미실행이라 조치 안내로 바꾼다
            hint = "LLM 서버(Ollama)에 연결할 수 없습니다. ollama serve 확인" if "Connect" in type(e).__name__ else repr(e)
            deps.session_store.append(thread_id, "assistant", "오류: " + hint)
            monitor_api.record(app.state, {**state, "message": message}, t0, user, model, error=hint[:200])
            yield sse({"type": "Error", "message": hint})
            yield "data: [DONE]\n\n"
            return
        report = state.get("report") or {}
        # 권한이 없어 답변에서 뺀 결정이 있으면 화면에 열람 요청 버튼을 띄우게 알린다
        if report.get("restricted"):
            yield sse({"type": "AccessDenied", "targets": report["restricted"]})
        final = state.get("final", "")
        # 의견 검토면 '결정으로 올리기' 버튼에 쓸 문장·주제·판정을 함께 보낸다
        opinion = None
        if state.get("mode") == "opinion" and report.get("items"):
            first = report["items"][0]
            opinion = {"statement": message, "topic": first["topic"], "status": first["review"]["status"]}
        yield sse({"type": "FinalAnswer", "reply": final, "mode": state.get("mode"),
                   "proposed": report.get("proposed", []), "opinion": opinion,
                   "schedule": state.get("schedule_draft")})     # 일정 추가 초안 → 화면의 확인 카드
        # 원장 근거로 답하는 모드만 저장. 일반 대화·일정은 저장하지 않는다
        if key and state.get("mode") in ("meeting", "interview", "opinion", "question"):
            # 지문은 처리 후 값으로 저장 (이번 처리에서 올린 승인 대기 결정까지 반영된 상태)
            replay = {"mode": state.get("mode"), "opinion": opinion, "restricted": report.get("restricted", []),
                      "refs": report.get("refs", []),
                      "events": [e for e in state.get("events", []) if e.get("type") != "Finalize"]}
            app.state.access.cache_put(key, await deps.mcp.call("ledger_fingerprint"), final, replay)
        deps.session_store.append(thread_id, "assistant", final)
        app.state.access.set_thread_refs(thread_id, report.get("refs") or [])
        after_run(user, thread_id, message, state)
        monitor_api.record(app.state, {**state, "message": message, "node_ms": node_ms}, t0, user, model)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- 캘린더·프로젝트·원장
@app.get("/meetings")
async def meetings(user: str):
    """
    회의 목록과 사용자별 열람 가능 여부 조회 (캘린더용)

    목록 표시는 1회 열람권을 쓰지 않는다 (consume 기본값 False).
    Args:
        user: 사용자 ID
    Returns:
        회의 정보에 can_view를 붙인 목록
    """
    ms = await refresh_meetings()
    return {"meetings": [{**m, "can_view": app.state.access.can_view_meeting(user, m)} for m in ms.values()]}


@app.get("/projects")
async def projects():
    """
    프로젝트 목록과 프로젝트별 결정·승인 대기·회의 수 조회
    Returns:
        프로젝트 정보에 decisions·pending·meetings 건수를 붙인 목록
    """
    rows = await app.state.deps.mcp.call("list_decisions", project_id="")
    out = []
    for p in app.state.access.projects:
        mine = [r for r in rows if r["project_id"] == p["project_id"]]
        out.append({**p, "decisions": len(mine), "pending": sum(r["status"] == "proposed" for r in mine),
                    "meetings": sum(m["project_id"] == p["project_id"] for m in app.state.meetings.values())})
    return {"projects": out}


@app.get("/projects/{key}/ledger")
async def project_ledger(key: str, user: str):
    """
    프로젝트 결정 원장 조회 — 권한 없는 결정은 문장을 가린다

    근거 원문(evidence)은 내려보내지 않고 근거 회의 ID만 준다. 목록 표시라 1회 열람권은 쓰지 않는다.
    Args:
        key: 프로젝트 키 (예: OPS)
        user: 사용자 ID
    Returns:
        프로젝트 정보와 결정 목록 (can_view, 가려진 statement, 근거 회의 ID 포함)
    Raises:
        HTTPException: 404 — 없는 프로젝트 키
    """
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
    """
    의견 검토 결과를 결정으로 올려 달라는 승인 요청

    원장에는 승인 대기(proposed)로만 올라가고, 승인은 팀장이 관리자 콘솔에서 한다 (HITL).
    Args:
        req: 제안자·결정 문장·주제·프로젝트·출처
    Returns:
        MCP propose_decision 결과 (proposed에 결정 ID)
    """
    name = app.state.access.user(req.user)["name"]
    r = await app.state.deps.mcp.call("propose_decision", statement=req.statement, topic=req.topic,
                                      decided_by=name, evidence=[], project_id=req.project_id)
    app.state.access.record_proposal(r["proposed"], req.user, req.source)
    monitor_api.link_decision(app.state, r["proposed"])     # 사람 신호(승인·반려)를 버전에 묶는다
    return r


@app.get("/admin/pending")
async def admin_pending(user: str):
    """
    관리자 콘솔 — 승인 대기 결정, 대기 중 열람 요청, 최근 처리 이력 조회 (팀장 전용)

    요청자는 승인 요청 기록이 있으면 그 사람, 없으면(원장에 직접 올라간 건) 결정의 decided_by.
    Args:
        user: 요청한 사용자 ID
    Returns:
        decisions(승인 대기 결정), requests(대기 열람 요청), history(최근 20건)
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    lead_only(user)
    acc = app.state.access
    rows = await app.state.deps.mcp.call("list_decisions", project_id="")
    src = acc.proposal_sources()
    pending = []
    for r in rows:
        if r["status"] != "proposed":
            continue
        did = r["decision_id"]
        requester = acc.user(src[did]["requested_by"])["name"] if did in src else r.get("decided_by")
        pending.append({k: v for k, v in r.items() if k != "evidence"} | {
            "requested_by": requester,
            "source": src.get(did, {}).get("source", "원장"),
            "meetings": sorted({e["meeting_id"] for e in r.get("evidence", [])}),
            # 승인하면 대체될 결정 미리보기 — 주제가 틀리면 무관한 결정이 무효가 되므로 누르기 전에 보여 준다
            "will_supersede": await app.state.deps.mcp.call("will_supersede", decision_id=did),
            "quotes": [e.get("quote") for e in r.get("evidence", [])][:2]} | await _decision_detail(r))
    requests = acc.requests("pending")
    for q in requests:                        # 열람 대상 회의 정보 — 펼쳤을 때 왜 못 보는지(참석자 아님) 바로 보이게
        q["meeting"] = await _meeting_brief(q["target"] if "-M" in q["target"] or "-I" in q["target"] else "")
    return {"decisions": pending, "requests": requests, "history": acc.requests(None)[:20]}


async def _meeting_brief(meeting_id: str) -> dict:
    """
    회의 요약 정보 (관리자 콘솔 세부사항용)
    Args:
        meeting_id: 회의 ID (비우면 빈 dict)
    Returns:
        {meeting_id, title, date, attendees:[이름(역할)]} 또는 빈 dict
    """
    if not meeting_id:
        return {}
    m = await app.state.deps.mcp.call("meeting_info", meeting_id=meeting_id)
    return {"meeting_id": meeting_id, "title": m.get("title", ""), "date": m.get("date", ""),
            "attendees": [f"{a['name']}({a['role']})" if a.get("role") else a["name"] for a in m.get("attendees", [])]} if m else {}


async def _decision_detail(d: dict) -> dict:
    """
    승인 대기 결정의 세부사항 — 근거 발언 전체, 근거 회의, 회의 정리 때의 라벨·해결 방향, 이 결정으로 이어진 이전 미결
    Args:
        d: 원장 결정 (evidence 포함)
    Returns:
        {evidence, meeting, label, label_reason, suggestions, prior}
    """
    h = await app.state.deps.mcp.call("decision_history", decision_id=d["decision_id"])
    items = h.get("items", [])
    cand = next((i for i in items if i["kind"] == "decision"), {})
    review = cand.get("review") or {}
    ev = d.get("evidence", [])
    return {"evidence": [{"meeting_id": e.get("meeting_id"), "seq": e.get("seq"), "quote": e.get("quote")} for e in ev],
            "meeting": await _meeting_brief(ev[0]["meeting_id"] if ev else ""),
            "label": cand.get("label"), "label_reason": review.get("label_reason"),
            "suggestions": review.get("suggestions", []),
            "prior": [{"meeting_id": i["meeting_id"], "date": i.get("date"), "statement": i["statement"]}
                      for i in items if i["kind"] == "open"]}


_BG: set = set()   # 응답 뒤에도 이어서 도는 작업 (참조를 쥐어 GC로 사라지지 않게)


@app.post("/decisions/{decision_id}/edit")
async def edit_decision(decision_id: str, req: EditReq):
    """
    승인 전 결정의 문장·주제 수정 (팀장 전용) — 원장 품질을 올리는 가장 싼 방법
    Args:
        decision_id: 승인 대기 결정 ID
        req:         수정 내용
    Returns:
        {"updated", "before", "after", "changed", "reindexed": 20초 안에 색인까지 끝났는지}
    Raises:
        HTTPException: 403 — 팀장이 아님 / 400 — 승인 대기 상태가 아님
    """
    lead_only(req.user)
    if req.topic and req.topic not in await _topic_keys():
        raise HTTPException(400, f"주제 목록에 없는 키입니다: {req.topic}")
    mcp = app.state.deps.mcp
    try:
        out = await mcp.call("update_proposed_decision", decision_id=decision_id,
                             actor=app.state.access.user(req.user)["name"],
                             statement=req.statement, topic=req.topic, rationale=req.rationale,
                             alternatives_rejected=req.alternatives, slots=req.slots, reindex=False)
    except Exception as e:  # pylint: disable=broad-exception-caught  # MCP 도구 오류 메시지를 그대로 전달
        raise HTTPException(400, str(e)) from e
    if isinstance(out, str):   # 도구 오류는 문자열로 돌아온다 (예: 승인 대기 아님)
        raise HTTPException(400, out)
    # 원장·이력은 이미 바뀌었다. 색인은 임베딩 모델이 바쁘면 오래 걸리므로 20초까지만 기다리고 나머지는 뒤에서 끝낸다
    task = asyncio.create_task(mcp.call("reindex_decision", decision_id=decision_id))
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    done, _ = await asyncio.wait({task}, timeout=20)
    # 도구 안의 오류(임베딩 시간 초과 등)는 예외가 아니라 오류 문장으로 돌아온다
    ok = bool(done) and task.exception() is None and isinstance(task.result(), dict)
    return {**out, "reindexed": ok}


class TopicReq(BaseModel):
    """
    새 주제 요청 본문
    Attributes:
        user:    요청한 사용자 ID (팀장)
        key:     주제 키 (예: notice.channel)
        desc:    설명
        aliases: 질문·발언에 나오는 표현
    """
    user: str
    key: str
    desc: str
    aliases: list[str]


class AliasReq(BaseModel):
    """
    별칭 보태기 요청 본문
    Attributes:
        user:    요청한 사용자 ID (팀장)
        aliases: 보탤 표현
    """
    user: str
    aliases: list[str]


def _tool_error(r) -> None:
    """
    MCP 도구 오류 문장이면 400으로 돌려준다
    Args:
        r: 도구 반환값
    Raises:
        HTTPException: 400 — 도구가 오류 문장을 돌려줬을 때
    """
    if isinstance(r, str):
        raise HTTPException(400, r.split(": ", 1)[-1])


@app.post("/topics")
async def add_topic(req: TopicReq):
    """
    새 주제 만들기 (팀장 전용) — 목록에 없는 사안이 나왔을 때. 모델은 주제를 만들지 않는다
    Args:
        req: 키·설명·별칭
    Returns:
        {"topic", "desc", "aliases", "reindexed"}
    Raises:
        HTTPException: 403 — 팀장이 아님 / 400 — 키 형식·중복·빈 설명·빈 별칭
    """
    lead_only(req.user)
    r = await app.state.deps.mcp.call("add_topic", key=req.key, desc=req.desc, aliases=req.aliases,
                                      actor=app.state.access.user(req.user)["name"])
    _tool_error(r)
    return r


@app.post("/topics/{topic}/aliases")
async def add_topic_aliases(topic: str, req: AliasReq):
    """
    기존 주제에 별칭 보태기 (팀장 전용) — 질문에 나온 표현을 색인이 찾지 못했을 때
    Args:
        topic: 주제 키
        req:   보탤 표현
    Returns:
        {"topic", "added", "reindexed"}
    Raises:
        HTTPException: 403 — 팀장이 아님 / 400 — 없는 주제·보탤 별칭 없음
    """
    lead_only(req.user)
    r = await app.state.deps.mcp.call("add_topic_aliases", topic=topic, aliases=req.aliases,
                                      actor=app.state.access.user(req.user)["name"])
    _tool_error(r)
    return r


async def _topics() -> list[dict]:
    """
    주제 목록 (키·설명) — MCP Resource ledger://topics
    Returns:
        [{"key", "desc"}]
    """
    text = await app.state.deps.mcp.resource("ledger://topics")
    return [{"key": k.strip(), "desc": v.strip()} for k, _, v in (ln.partition(":") for ln in text.splitlines()) if k.strip()]


async def _topic_keys() -> set[str]:
    """주제 키 집합"""
    return {x["key"] for x in await _topics()}


def _value_warnings(statement: str, evidence: list[dict]) -> list[str]:
    """
    결정 문장의 숫자·단위가 근거 발언에 있는지 (없으면 경고)
    Args:
        statement: 결정 문장
        evidence:  근거 [{quote}]
    Returns:
        경고 문장 목록
    """
    from agent.enrich import values_ok  # pylint: disable=import-outside-toplevel
    bad = values_ok(statement, " ".join(e.get("quote") or "" for e in evidence))
    return [f"근거 발언에 없는 값: {', '.join(bad)}"] if bad else []


async def _same_topic(project_id: str, topic: str, exclude: str) -> list[dict]:
    """
    같은 프로젝트·주제의 다른 결정 (상태 포함, 날짜순)
    Args:
        project_id: 프로젝트 ID
        topic:      주제 키
        exclude:    빼고 볼 결정 ID
    Returns:
        [{decision_id, statement, status, superseded_by, effective_from}]
    """
    rows = await app.state.deps.mcp.call("list_decisions", project_id=project_id)
    keep = ("decision_id", "statement", "status", "superseded_by", "effective_from")
    return sorted([{k: d.get(k) for k in keep} for d in rows if d.get("topic") == topic and d["decision_id"] != exclude],
                  key=lambda d: d.get("effective_from") or "9999")


@app.get("/decisions/{decision_id}/edit-context")
async def edit_context(decision_id: str, user: str):
    """
    결정 수정 화면의 재료 — 근거 발언 앞뒤 원문, 근거 회의, 주제 목록·추천, 같은 주제 결정, 대체 미리보기, 근거 대조 경고
    Args:
        decision_id: 승인 대기 결정 ID
        user:        요청한 사용자 ID (팀장)
    Returns:
        {decision, context:[{meeting, segments, evidence_seqs}], topics, same_topic, will_supersede, warnings}
    Raises:
        HTTPException: 403 — 팀장이 아님 / 404 — 없는 결정
    """
    lead_only(user)
    mcp = app.state.deps.mcp
    d = (await mcp.call("decision_history", decision_id=decision_id)).get("decision")
    if not d:
        raise HTTPException(404, "없는 결정입니다.")
    context = []
    for mid in dict.fromkeys(e["meeting_id"] for e in d.get("evidence", []) if e.get("meeting_id")):
        seqs = sorted(e["seq"] for e in d["evidence"] if e.get("meeting_id") == mid and e.get("seq"))
        lo, hi = (max(1, seqs[0] - 3), seqs[-1] + 3) if seqs else (1, 0)
        context.append({"meeting": await _meeting_brief(mid), "evidence_seqs": seqs, "from": lo, "to": hi,
                        "segments": await mcp.call("get_segments", meeting_id=mid, seq_from=lo, seq_to=hi) if seqs else []})
    # 주제 추천은 임베딩이 필요해 느릴 수 있어 /topic-suggest로 따로 받는다 (패널은 바로 뜬다)
    return {"decision": d, "context": context, "topics": await _topics(),
            "same_topic": await _same_topic(d["project_id"], d["topic"], decision_id),
            "will_supersede": await mcp.call("will_supersede", decision_id=decision_id),
            "warnings": _value_warnings(d["statement"], d.get("evidence", []))}


@app.get("/decisions/{decision_id}/topic-suggest")
async def topic_suggest(decision_id: str, user: str, text: str):
    """
    문장 기준 주제 추천 (임베딩 유사도 상위 5개). 모델이 바쁘면 기다리지 않고 빈 목록
    Args:
        decision_id: 승인 대기 결정 ID (기록용)
        user:        요청한 사용자 ID (팀장)
        text:        결정 문장
    Returns:
        {"suggested": [{topic, alias | score}], "note": 건너뛴 이유} — 별칭 일치가 앞
    """
    lead_only(user)
    # 별칭 일치를 먼저 — 임베딩 유사도는 결정·문서가 있는 주제만 걸려, 결정이 아직 없는 주제(새 주제 포함)를 놓친다
    hits = await app.state.deps.mcp.call("alias_topics", text=text)
    out = [{"topic": h["topic"], "alias": h["alias"]} for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []
    note = ""
    try:
        got = await asyncio.wait_for(app.state.deps.mcp.call("topic_candidates", text=text, k=5), timeout=15)
        out += [c for c in got if c.get("topic") and c["topic"] not in {x["topic"] for x in out}]
    except asyncio.TimeoutError:
        note = "임베딩 모델이 바빠 유사도 추천은 건너뜀"
    return {"suggested": out[:6], "note": note if not out else ""}


@app.get("/decisions/{decision_id}/edit-preview")
async def edit_preview(decision_id: str, user: str, topic: str = "", statement: str = ""):
    """
    수정 중 영향 미리보기 — 바꾼 주제로 승인하면 대체될 결정, 그 주제의 기존 결정, 바꾼 문장의 근거 대조 경고
    Args:
        decision_id: 승인 대기 결정 ID
        user:        요청한 사용자 ID (팀장)
        topic:       가정할 주제 키
        statement:   고친 문장
    Returns:
        {will_supersede, same_topic, warnings, topic_ok}
    Raises:
        HTTPException: 403 — 팀장이 아님 / 404 — 없는 결정
    """
    lead_only(user)
    mcp = app.state.deps.mcp
    d = (await mcp.call("decision_history", decision_id=decision_id)).get("decision")
    if not d:
        raise HTTPException(404, "없는 결정입니다.")
    t = topic or d["topic"]
    return {"will_supersede": await mcp.call("will_supersede", decision_id=decision_id, topic=t),
            "same_topic": await _same_topic(d["project_id"], t, decision_id),
            "warnings": _value_warnings(statement or d["statement"], d.get("evidence", [])),
            "topic_ok": t in await _topic_keys()}


@app.get("/decisions/{decision_id}/segments")
async def decision_segments(decision_id: str, user: str, meeting: str, seq_from: int, seq_to: int):
    """
    수정 화면에서 원문 범위 넓히기 (앞 더 보기 / 뒤 더 보기) — 그 결정의 근거 회의만
    Args:
        decision_id: 결정 ID
        user:        요청한 사용자 ID (팀장)
        meeting:     근거 회의 ID
        seq_from:    시작 발언 번호
        seq_to:      끝 발언 번호
    Returns:
        {"segments": [...]}
    Raises:
        HTTPException: 403 — 팀장이 아님 / 400 — 이 결정의 근거 회의가 아님
    """
    lead_only(user)
    d = (await app.state.deps.mcp.call("decision_history", decision_id=decision_id)).get("decision") or {}
    if meeting not in {e.get("meeting_id") for e in d.get("evidence", [])}:
        raise HTTPException(400, "이 결정의 근거 회의가 아닙니다.")
    return {"segments": await app.state.deps.mcp.call("get_segments", meeting_id=meeting,
                                                      seq_from=max(1, seq_from), seq_to=seq_to)}


@app.get("/open-items")
async def open_items(user: str):
    """
    미결함 — 열린 미결과 경과일, 같은 회의의 후속 할 일 (팀장 전용)

    후속 할 일의 기한이 지났거나 2주 넘게 열려 있으면 'stale'로 표시해 팀장에게 결정 여부를 묻는다.
    침묵을 결정으로 보지 않기 위한 장치.
    Args:
        user: 요청한 사용자 ID
    Returns:
        {"items": [미결 + age_days, follow_up, stale]}
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    lead_only(user)
    mcp, out, today = app.state.deps.mcp, [], date.fromisoformat(TODAY)
    for o in await mcp.call("open_items", project_id=""):
        acts = [a for a in await mcp.call("meeting_items", meeting_id=o["meeting_id"])
                if a["kind"] == "action" and _overlap(a["statement"], o["statement"]) >= 1]
        age = (today - date.fromisoformat(o["date"])).days if o.get("date") else 0
        overdue = [a for a in acts if a.get("due") and a["due"] < TODAY]
        out.append({**o, "age_days": age, "follow_up": [{"statement": a["statement"], "owner": a.get("owner"),
                                                         "due": a.get("due")} for a in acts],
                    "stale": bool(overdue) or age >= 14})
    return {"items": out}


@app.post("/open-items/close")
async def close_open_item(req: CloseReq):
    """
    미결 닫기 (팀장 전용) — 회의 밖에서 정해졌거나 하지 않기로 한 것
    Args:
        req: 닫을 미결과 사유
    Returns:
        갱신된 미결 항목
    Raises:
        HTTPException: 403 — 팀장이 아님 / 400 — 열린 미결이 아님
    """
    lead_only(req.user)
    try:
        return await app.state.deps.mcp.call("close_open_item", item_id=req.item_id,
                                             actor=app.state.access.user(req.user)["name"], reason=req.reason)
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(400, str(e)) from e


@app.post("/decisions/{decision_id}/approve")
async def approve(decision_id: str, req: UserReq):
    """
    승인 대기 결정 승인 (팀장 전용)

    같은 주제의 기존 유효 결정은 원장에서 대체(superseded) 처리되고, 제안자에게 알림이 간다.
    Args:
        decision_id: 결정 ID
        req: 승인하는 사용자
    Returns:
        MCP approve_decision 결과 (superseded에 대체된 결정 ID)
    Raises:
        HTTPException: 403 — 팀장이 아님 / 400 — 원장이 승인을 거부함(오류 메시지 전달)
    """
    lead_only(req.user)
    acc = app.state.access
    r = await app.state.deps.mcp.call("approve_decision", decision_id=decision_id,
                                      approved_by=f"{acc.user(req.user)['name']} {acc.user(req.user)['role']}")
    if isinstance(r, str):                       # 도구 오류(예: 주제 없는 결정)는 문장으로 돌아온다
        raise HTTPException(400, r.split(": ", 1)[-1])
    if isinstance(r, dict) and r.get("error"):
        raise HTTPException(400, r["error"])
    acc.after_decision(decision_id, req.user, True, r.get("superseded", []))
    return r


@app.post("/decisions/{decision_id}/reject")
async def reject(decision_id: str, req: RejectReq):
    """
    승인 대기 결정 반려 (팀장 전용)

    원장에는 rejected로 남고, 제안자에게 사유와 함께 알림이 간다.
    Args:
        decision_id: 결정 ID
        req: 반려하는 사용자와 사유
    Returns:
        MCP reject_decision 결과
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    lead_only(req.user)
    acc = app.state.access
    r = await app.state.deps.mcp.call("reject_decision", decision_id=decision_id,
                                      rejected_by=acc.user(req.user)["name"], reason=req.reason)
    acc.after_decision(decision_id, req.user, False, [], req.reason)
    return r


@app.post("/access-requests")
async def access_request(req: AccessReq):
    """
    회의·결정 열람 요청 등록
    Args:
        req: 요청자·대상·사유
    Returns:
        생성된 열람 요청 ID
    """
    return {"id": app.state.access.request_access(req.user, req.target, req.reason)}


@app.post("/access-requests/{req_id}/decide")
async def access_decide(req_id: int, req: DecideReq):
    """
    열람 요청 승인·반려 (팀장 전용)

    범위(요청자만 / 같은 역할 / 전체)와 기간(1회 / 프로젝트 기간 / 무기한 / 지정일까지)을 함께 정한다.
    Args:
        req_id: 열람 요청 ID
        req: 처리자·승인 여부·범위·기간
    Returns:
        처리 결과 (id, status, scope, duration)
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    lead_only(req.user)
    return app.state.access.decide_request(req_id, req.user, req.approve, req.scope, req.duration, req.until)


@app.get("/admin/acl")
async def admin_acl(user: str):
    """
    회의별 역할 접근 권한 조회 (팀장 전용)
    Args:
        user: 요청한 사용자 ID
    Returns:
        회의 정보에 열람 허용 역할(roles)을 붙인 목록
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    lead_only(user)
    acl = app.state.access.acl()
    return {"meetings": [{**m, "roles": acl.get(m["meeting_id"], [])} for m in app.state.meetings.values()]}


@app.post("/admin/acl")
async def admin_acl_set(req: AclReq):
    """
    회의별 역할 접근 권한 변경 (팀장 전용)

    참석자가 아니어도 허용된 역할이면 그 회의 원문·결정을 볼 수 있게 된다.
    Args:
        req: 처리자·회의·역할·허용 여부
    Returns:
        처리 완료 표시
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    lead_only(req.user)
    app.state.access.set_acl(req.meeting_id, req.role, req.on, req.user)
    return {"ok": True}


# ---------------------------------------------------------------- 알림·RAG
# ---------------------------------------------------------------- 모델 조합 목록 (입력창 아래 선택용)
@app.get("/models")
async def models():
    """
    모델 조합 목록과 사용 가능 여부 조회 (입력창 아래 선택용)

    조합 그래프가 만들어져 있고 두 모델이 모두 Ollama에 설치되어 있어야 available.
    Ollama에 닿지 않으면 설치 목록을 빈 것으로 보고 전부 사용 불가로 표시한다.
    Returns:
        presets(조합 목록), default(기본 조합 키)
    """
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            installed = {m["name"] for m in (await c.get(f"{OLLAMA_URL}/api/tags")).json().get("models", [])}
    except (httpx.HTTPError, ValueError):     # Ollama 꺼짐·응답 형식 오류
        installed = set()
    out = [{"key": k, "label": lab, "fast": f, "quality": q, "available": k in app.state.graphs and {f, q} <= installed}
           for k, (lab, f, q) in PRESETS.items()]
    return {"presets": out, "default": "auto"}


@app.get("/notifications")
async def notifications(user: str):
    """
    사용자 알림 조회 (최근 순)
    Args:
        user: 사용자 ID
    Returns:
        알림 목록과 읽지 않은 수
    """
    rows = app.state.access.notifications(user)
    return {"notifications": rows, "unread": sum(not r["read"] for r in rows)}


@app.post("/notifications/read")
async def notifications_read(req: UserReq):
    """
    사용자 알림 전체 읽음 처리
    Args:
        req: 사용자
    Returns:
        처리 완료 표시
    """
    app.state.access.mark_read(req.user)
    return {"ok": True}


@app.get("/rag/counts")
async def rag_counts():
    """
    RAG 저장소(R1~R4)별 적재 건수 조회
    Returns:
        컬렉션별 건수
    """
    return await app.state.deps.mcp.call("rag_counts")


@app.get("/rag/search")
async def rag_search(collection: str, q: str, filtered: bool = True, k: int = 6):
    """
    RAG 저장소 검색 결과 들여다보기 (설정 화면)

    filtered=False로 대체된 결정·구버전 문서까지 보여 주어 필터 효과를 비교한다.
    Args:
        collection: 컬렉션 — r1(회의 원문) | r2(문서) | r3(결정) | r4(정책)
        q: 검색어
        filtered: 현행만 볼지 여부
        k: 결과 수
    Returns:
        검색 결과 행 목록
    """
    rows = await app.state.deps.mcp.call("rag_inspect", collection=collection, query=q, filtered=filtered, k=k)
    return {"rows": rows}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
