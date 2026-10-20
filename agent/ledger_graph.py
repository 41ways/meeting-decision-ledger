"""결정 원장 Agent 그래프 — 베이스(all_total_vue의 agent/graph.py)와 같은 노드 이름·순서를 쓴다.

route ─┬ chat ─────────────────────────────────────────────── chat_answer → END
       ├ schedule ─────────────────────────────────────────── schedule_answer → END  (LLM 없이 회의 목록으로 답함)
       ├ schedule_add ─────────────────────────────────────── schedule_add → END  (초안만, 저장은 사람이 '추가')
       └ meeting / interview / opinion / question
            → plan → gate → research ⇄ replan_check → analysis → write ⇄ validate → finalize

노드         | 베이스(장애 진단)          | 이 프로젝트
-------------|----------------------------|------------------------------------------------
route        | chat / 파이프라인 분기     | chat / 회의 정리 / 의견 검토 / 근거 질의
plan         | MCP Prompt + 태스크 분해   | MCP Prompt + 회의록에서 결정 후보 추출(발언 번호만)
gate         | analysis 태스크 코드 제거  | 근거 없는 결정·회상 발언을 코드로 제거, 인용은 원문에서 채움
research     | 모니터링·런북 병렬 조회    | 결정마다 현재 기준(R4>R3>R2)·권한·예외를 병렬 조회
replan_check | 런북 없으면 재조사         | 기준이 없으면 회의 원문(R1) 논의를 추가 조회
analysis     | 수집 결과 비교 분석        | 충돌 판정 (LLM) + 코드 가드레일
write        | 고품질 모델로 답변 작성    | 회의 보고서(템플릿) / 질의 답변(LLM, 토큰 스트리밍)
validate     | 30자·번호 검사 → 재작성    | 인용 원문 일치·답변 근거 ID 검사 → 재작성
finalize     | 최종 답변                  | 최종 답변 + (옵션) 승인 대기로 원장 등록

면접 전사문(HBE-I01 등)도 같은 노드를 탄다. plan이 질문·답변 요약 쌍과 공정성 점검 후보(금지 질문·면접 중 합격 확정)를
만들고, 나머지(gate·research·analysis·validate)는 회의와 같은 코드가 처리한다. 채용 판단은 하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from functools import partial
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from agent.schedule_add import events_as_meetings, schedule_add_node
from ledger.schedule_parse import is_add_request

TODAY = "2026-10-20"
_PRJ_FILE = Path(__file__).resolve().parent.parent / "data" / "project" / "projects.json"
# 프로젝트 이름표 (질문 대상 프로젝트 고르기·표시용). 약자·이름은 projects.json 한 곳에서 관리
# HBE(면접 전사문)는 결정 질의 대상 프로젝트가 아니라서 이름표에서 뺀다
PROJECTS = {p["project_id"]: f"{p['name']}({'현재' if p['status'] == '진행' else '과거'} 프로젝트, {p['key']})"
            for p in json.loads(_PRJ_FILE.read_text())["projects"] if p["key"] != "HBE"}
_PRJ_ALL = json.loads(_PRJ_FILE.read_text())["projects"]
KEY_TO_PID = {p["key"]: p["project_id"] for p in _PRJ_ALL}
PID_TO_KEY = {v: k for k, v in KEY_TO_PID.items()}
DEFAULT_PID = "PRJ-OPSAI"
MEETING_ID = re.compile(r"\b(?:([A-Z]{3})-)?([MI])(\d{2})\b")   # OPS-M04, HBE-I01, 또는 줄여서 M04·I01


def normalize_meeting_id(msg: str) -> str:
    """
    메시지 속 회의·면접 ID를 '약자-M04' 형태로 정규화

    'M04'처럼 약자를 빼고 써도 받는다 (회의는 OPS, 면접은 HBE가 기본).
    Args:
        msg: 사용자 메시지
    Returns:
        정규화한 ID (예: 'OPS-M04'). ID가 없으면 빈 문자열
    """
    m = MEETING_ID.search(msg)
    if not m:
        return ""
    key = m.group(1) or ("HBE" if m.group(2) == "I" else "OPS")
    return f"{key}-{m.group(2)}{m.group(3)}"

# 결정 판별 규칙 — gate가 코드로 확인한다 (회상·제안·보류 발언을 결정으로 뽑는 오류 차단)
CONFIRM = re.compile(r"걸로|겠습니다|확정|하죠|합시다|바꾸죠|가죠|그렇게 하|갑시다")
NOT_CONFIRM = re.compile(r"확정은 아닙|아직 확정|결론 내지|다음에 다시|보고 정할|검토해")
CHAIR_ROLES = ("PM", "PL")      # 진행자급의 단정 발언('코드에서 걸러요')도 결정으로 인정


def is_decision_utterance(seg: dict) -> bool:
    """
    발언이 결정(확정) 발언인지 규칙으로 판별

    보류 표현이 있으면 확정 표현이 섞여 있어도 결정이 아니다.
    보류 표현이 없으면 확정 표현이 있거나 진행자(PM·PL) 발언일 때 결정으로 본다.
    Args:
        seg: 회의 발언 (text·role 포함)
    Returns:
        결정 발언이면 True
    """
    # 보류를 먼저 본다 — '아직 확정은 아닙니다'에도 '확정'이 들어 있어 CONFIRM만 보면 결정으로 잡힌다
    if NOT_CONFIRM.search(seg["text"]):
        return False
    return bool(CONFIRM.search(seg["text"])) or seg["role"] in CHAIR_ROLES

# 주제 → 권한표(R4 AUTH-*) 주제. 목록에 없으면 기술 선택(tech.stack)
AUTH_TOPIC = {"schedule": "schedule", "budget": "budget", "perf.latency": "scope", "scope.core": "scope",
              "answer.grounding": "scope", "llm.external": "security", "meeting.retention": "security"}


@dataclass
class LedgerDeps:
    """
    그래프 노드가 쓰는 외부 의존성 묶음

    Wave 13 — 의존성 주입. 노드는 이것만 보고 동작한다 (테스트에서 가짜로 갈아 끼움).
    """
    fast_llm: Any              # Wave 10a — 분류·주제 선택
    quality_llm: Any           # Wave 10a — 추출·충돌 판정·답변 작성
    mcp: Any                   # Wave 2  — 결정 원장 MCP 서버
    session_store: Any = None  # Wave 12
    chat_llm: Any = None       # 일반 대화 전용 모델 (자동 모드에서 작은 모델). 없으면 fast_llm
    can_view: Any = None       # (user_id, decision) -> bool. 권한 없는 결정은 내용 자체를 LLM에 넘기지 않는다
    calendar: Any = None       # CalendarProvider (ledger/calendar.py). 일정 추가 초안·일정 조회에 쓴다
    today: str = TODAY
    propose_to_ledger: bool = False


class LedgerState(TypedDict, total=False):
    """
    그래프 상태. 노드는 바꿀 키만 dict로 돌려주고 LangGraph가 합친다

    total=False라서 경로에 따라 채워지지 않는 키가 있다 (chat 경로는 items 없음 등).
    """
    message: str
    user_id: str
    scope_project: str        # 입력창 아래에서 사용자가 고른 프로젝트 약자 (없으면 자동)
    scope_meeting: str        # 입력창 아래에서 고른 회의 (없으면 지정 안 함)
    history: list[dict]
    restricted: list[str]     # 열람 권한이 없어 가린 결정 ID
    answer_sources: str       # 질의 답변에 쓴 근거 글 (품질 심판용)
    mode: str                 # meeting / interview / opinion / question / chat / schedule / schedule_add
    schedule_draft: dict      # 일정 추가 초안 (저장 전)
    meeting_id: str
    meeting_date: str
    attendee_roles: list[str]
    segments: list[dict]
    items: list[dict]         # 검토 대상: 회의 결정들, 또는 의견·질문 1건
    action_items: list[dict]
    undecided: list[dict]
    overview: dict            # 회의 요약: 주제 · 논의 요약 · 방향성
    competencies: list[dict]  # 면접 전사문: 질문·답변 요약 쌍
    replan_rounds: int
    draft: str
    validation_attempts: int
    validation_feedback: str | None
    report: dict
    final: str
    usage: dict
    events: list[dict]


def _ev(state, *evs) -> list[dict]:
    """
    기존 이벤트 목록 뒤에 새 이벤트를 붙인 새 목록 생성

    상태의 events를 직접 바꾸지 않고 새 리스트를 돌려준다 (노드 반환값으로 덮어쓰는 방식).
    Args:
        state: 현재 그래프 상태
        *evs: 추가할 이벤트 dict들
    Returns:
        누적 이벤트 목록
    """
    return state.get("events", []) + list(evs)


async def gather_safe(coros, fallback, limit: int = 2):
    """
    코루틴 병렬 실행 + 에러 격리

    한 건이 실패해도 나머지 결과로 보고서를 만든다 (실패분은 fallback).
    Args:
        coros: 실행할 코루틴 목록
        fallback: (순번, 예외) -> 대체 결과. 실패한 건에만 호출
        limit: 로컬 LLM 동시 호출 상한. 무제한 gather는 Ollama 대기열만 길어진다
    Returns:
        입력 순서대로 정렬된 결과 목록 (실패분은 fallback 결과)
    """
    sem = asyncio.Semaphore(limit)

    async def run(c):
        """
        세마포어 안에서 코루틴 하나 실행
        Args:
            c: 실행할 코루틴
        Returns:
            코루틴 결과
        """
        async with sem:
            return await c
    # return_exceptions=True — 예외를 결과 자리에 받아야 순번별로 fallback을 끼울 수 있다
    results = await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
    return [fallback(i, r) if isinstance(r, Exception) else r for i, r in enumerate(results)]


def _usage(state, *us) -> dict:
    """
    LLM 호출 수·토큰 사용량 누적
    Args:
        state: 현재 그래프 상태 (기존 usage)
        *us: LLM 호출별 사용량 dict. None은 LLM을 부르지 않은 경우라 건너뛴다
    Returns:
        누적한 usage dict (llm_calls, tokens)
    """
    u = dict(state.get("usage") or {"llm_calls": 0, "tokens": 0})
    for x in us:
        if x:
            # extra_calls — 한 노드 안에서 추가로 부른 호출(예외 범위 문항)을 한 usage에 합쳐 온 경우
            u["llm_calls"] += 1 + x.get("extra_calls", 0)
            u["tokens"] += x.get("tokens", 0)
    return u


# ---------------------------------------------------------------- route
# 일정 질문 판별: 상대 주(이번·지난·N주 전), 상대 날짜(오늘·어제), 월, 개수('몇 개') 표현
SCHEDULE_Q = re.compile(r"(이번|지난|지지난|저번|저저번|다음|다다음)\s*주|전전?주|\d+\s*주\s*(전|후|뒤)"
                        r"|오늘|내일|어제|이번\s*달|지난\s*달|다음\s*달|\d{1,2}월|몇\s*(개|건|번)|언제\s*있")

ROUTE_SCHEMA = {"type": "object", "properties": {"mode": {"type": "string",
                "enum": ["meeting", "opinion", "question", "chat"]}}, "required": ["mode"]}


@traceable(name="route_node")
async def route_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    요청 종류(mode)를 고르는 라우팅 노드

    Wave 5 — 쿼리 라우팅. 분명한 경우는 규칙으로, 애매하면 저비용 모델이 고른다.
    Args:
        state: 그래프 상태 (message, scope_meeting 사용)
        deps: 의존성 (fast_llm)
    Returns:
        mode, meeting_id, usage, events(RouteDecided) 갱신
    """
    msg = state["message"]
    # 메시지에 ID가 없으면 입력창에서 고른 회의를 쓴다
    mid = normalize_meeting_id(msg) or (state.get("scope_meeting") or "")
    usage = None
    # 규칙 순서가 우선순위 — 일정 추가 > 일정 > 회의 정리 > 의견 > 질의. 앞 규칙이 더 좁은 조건이다
    # 일정 추가: 날짜 + (추가 동사와 시각·일정 단어, 또는 시각). 묻는 문장은 조회로 넘긴다
    if is_add_request(msg):
        mode = "schedule_add"
    # 일정: 기간 표현 + 회의·면접 단어. '지난주 회의 정리해줘'는 일정이 아니라 회의 정리라서 정리·요약 단어가 있으면 제외
    elif SCHEDULE_Q.search(msg) and re.search(r"회의|면접|일정|미팅", msg) and not re.search(r"정리|요약|추출", msg):
        mode = "schedule"
    # 회의 정리: 회의 ID가 있고 정리 동사가 있을 때만. ID만 있는 질문('M04에서 왜 바꿨어?')은 질의로 넘긴다
    elif mid and re.search(r"정리|요약|추출|처리|분석", msg):
        mode = "interview" if "-I" in mid else "meeting"
    # 의견: 제안·권유 어미 ('바꾸면 어때?', '하자')
    elif re.search(r"어떨까|어때|바꾸면|바꾸자|하자|쓰면|쓰자|제안|넣자", msg):
        mode = "opinion"
    # 질의: 과거 결정의 이유·시점·주체를 묻는 표현
    elif re.search(r"왜|언제|누가|정해졌|하기로|했어요\?|했나|이유|몇 초|어떻게 됐", msg):
        mode = "question"
    else:
        out, usage = await deps.fast_llm.json(
            "메시지를 분류하라. meeting: 회의록(OPS-M01 등) 정리 요청 / opinion: 새 제안 검토 / "
            "question: 과거 결정·근거 질문 / chat: 인사·잡담·사용법", msg, ROUTE_SCHEMA)
        mode = out.get("mode", "chat")
    return {"mode": mode, "meeting_id": mid, "usage": _usage(state, usage),
            "events": _ev(state, {"type": "RouteDecided", "route": mode})}


def _range(msg: str, today: str) -> tuple[str, str, str]:
    """
    질문 속 기간 표현을 날짜 구간으로 변환

    주는 월~일 기준. 기간 표현이 없으면 이번 주.
    Args:
        msg: 사용자 메시지
        today: 기준 날짜 (YYYY-MM-DD)
    Returns:
        (시작일, 종료일, 화면 표시용 이름표) — 날짜는 ISO 문자열
    Examples:
        today가 2026-10-20(화)이고 '지난주 회의'면 ('2026-10-12', '2026-10-18', '지난주')
    """
    t = date.fromisoformat(today)
    mon = t - timedelta(days=t.weekday())
    # 주 단위: '지지난주·저저번주·전전주' = 2주 전, '다다음 주' = 2주 뒤, 'N주 전/후'는 숫자대로 (월~일 기준)
    # '지지난'·'다다음'을 '지난'·'다음'보다 먼저 검사해야 한다 ('지지난주'에도 '지난주'가 들어 있음)
    weeks = None
    if m := re.search(r"(\d+)\s*주\s*(전|후|뒤)", msg):
        weeks = -int(m.group(1)) if m.group(2) == "전" else int(m.group(1))
    elif re.search(r"지지난\s*주|저저번\s*주|전전주", msg):
        weeks = -2
    elif re.search(r"지난\s*주|저번\s*주|전주", msg):
        weeks = -1
    elif re.search(r"다다음\s*주", msg):
        weeks = 2
    elif re.search(r"다음\s*주", msg):
        weeks = 1
    if weeks is not None:
        names = {-2: "지지난주", -1: "지난주", 1: "다음 주", 2: "다다음 주"}
        a, b = mon + timedelta(days=7 * weeks), mon + timedelta(days=7 * weeks + 6)
        label = names.get(weeks, f"{abs(weeks)}주 {'전' if weeks < 0 else '뒤'}")
    elif "오늘" in msg:
        a = b = t
        label = "오늘"
    elif "내일" in msg:
        a = b = t + timedelta(days=1)
        label = "내일"
    elif "어제" in msg:
        a = b = t - timedelta(days=1)
        label = "어제"
    elif m := re.search(r"(\d{1,2})월", msg):
        # 월말 = 다음 달 1일 - 1일. 12월은 다음 해 1월 1일에서 뺀다
        mm = int(m.group(1))
        a = date(t.year, mm, 1)
        b = date(t.year + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)
        label = f"{mm}월"
    elif re.search(r"(이번|지난|다음)\s*달", msg):
        # 현재 구현은 '지난 달'·'다음 달'도 이번 달 구간으로 계산한다
        a = t.replace(day=1)
        b = a.replace(month=a.month % 12 + 1) - timedelta(days=1)
        label = "이번 달"
    else:
        a, b, label = mon, mon + timedelta(days=6), "이번 주"
    return a.isoformat(), b.isoformat(), label


@traceable(name="schedule_answer_node")
async def schedule_answer_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    일정 질문에 회의 목록으로 답하는 노드

    일정 질문은 LLM 없이 회의 목록(MCP Tool)으로 코드가 답한다 — 지어낼 여지가 없다.
    Args:
        state: 그래프 상태 (message 사용)
        deps: 의존성 (mcp, today)
    Returns:
        final(일정 목록 답변), events(ScheduleAnswered, Finalize) 갱신
    """
    a, b, label = _range(state["message"], deps.today)
    # ISO 날짜 문자열은 사전순 비교가 곧 날짜 비교
    ms = [m for m in await deps.mcp.call("list_meetings") if a <= m["date"] <= b]
    ms += events_as_meetings(deps.calendar, a, b)       # 사람이 확인해 추가한 일정 (EVT-*)
    q = state["message"]
    if re.search(r"면접", q):
        ms, kind = [m for m in ms if m.get("domain") == "interview"], "면접은"
    elif re.search(r"일정", q):
        kind = "일정은"            # 일정 = 회의 + 면접 + 추가된 기타 일정 전부
    else:
        ms, kind = [m for m in ms if m.get("domain") not in ("interview", "event")], "회의는"
    days = "월화수목금토일"
    n_int = sum(m.get("domain") == "interview" for m in ms)
    n_evt = sum(m.get("domain") == "event" for m in ms)
    etc = f", 기타 일정 {n_evt}건" if n_evt else ""
    detail = f" (회의 {len(ms) - n_int - n_evt}건, 면접 {n_int}건{etc})" if kind == "일정은" and ms else ""
    span = f"{a[5:]}~{b[5:]}" if a != b else a[5:]
    head = f"{label}({span}) {kind} {len(ms)}건입니다{detail}."
    # 참석자에서 지원자는 뺀다 (면접 지원자 이름을 일정 답변에 노출하지 않음)
    lines = [head]
    for m in sorted(ms, key=lambda x: (x["date"], x.get("start") or "")):
        weekday = days[date.fromisoformat(m["date"]).weekday()]
        attendees = ", ".join(x for x in m["attendees"] if not x.startswith("지원자"))
        time = f" {m['time']}" if m.get("time") else ""
        lines.append(f"- {m['meeting_id']} · {m['date'][5:]}({weekday}){time} {m['title']} · 참석 {attendees}")
    return {"final": "\n".join(lines),
            "events": _ev(state, {"type": "ScheduleAnswered", "range": f"{a}~{b}", "count": len(ms)},
                          {"type": "Finalize", "pipeline": "schedule"})}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    일반 대화 응답 노드

    Wave 5 direct 경로 — 세션 히스토리 기반 짧은 응답 (Wave 12 세션).
    회의·결정 사실은 지어내지 않고 기능 사용법으로 안내한다.
    Args:
        state: 그래프 상태 (message, history 사용)
        deps: 의존성 (chat_llm, 없으면 fast_llm)
    Returns:
        final(응답), usage, events(Finalize) 갱신
    """
    # 최근 6개만 — 작은 모델의 문맥을 짧게 유지
    hist = "\n".join(f"{h['role']}: {h['content']}" for h in (state.get("history") or [])[-6:])
    writer = get_stream_writer()
    reply, usage = await (deps.chat_llm or deps.fast_llm).text(
        "너는 회의 요약·결정 정리 비서다. 한두 문장으로 답한다. 할 수 있는 일: 회의록 정리('OPS-M04 회의 정리해줘'), "
        "제안 검토('Redis로 바꾸면 어때?'), 과거 결정 근거 질의('SSE는 왜 골랐어?'), 일정 질문('이번 주 회의 몇 개야?'), "
        "일정 추가('다음 주 화요일 오후 3시 OPS 주간 회의 잡아줘'). "
        "회의·결정·일정에 관한 사실은 이 대화에서 지어내지 말고, 위 기능으로 다시 물어보라고 안내한다.",
        f"{hist}\nuser: {state['message']}", on_token=lambda p: writer({"type": "AnswerToken", "delta": p}))
    return {"final": reply, "usage": _usage(state, usage),
            "events": _ev(state, {"type": "Finalize", "pipeline": "chat"})}


# ---------------------------------------------------------------- plan
EXTRACT_SCHEMA = {"type": "object", "properties": {
    "overview": {"type": "object", "properties": {
        "topic": {"type": "string"}, "discussion": {"type": "array", "items": {"type": "string"}},
        "direction": {"type": "string"}}, "required": ["topic", "discussion", "direction"]},
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "seq": {"type": "integer"}, "proposal_seq": {"type": "integer"},
        "statement": {"type": "string"}, "topic": {"type": "string"}},
        "required": ["seq", "proposal_seq", "statement", "topic"]}},
    "action_items": {"type": "array", "items": {"type": "object", "properties": {
        "seq": {"type": "integer"}, "statement": {"type": "string"},
        "owner": {"type": ["string", "null"]}, "due": {"type": ["string", "null"]}},
        "required": ["seq", "statement", "owner", "due"]}},
    "undecided": {"type": "array", "items": {"type": "object", "properties": {
        "seq": {"type": "integer"}, "subject": {"type": "string"}}, "required": ["seq", "subject"]}}},
    "required": ["overview", "decisions", "action_items", "undecided"]}

# 할 일 배정 표현 (기한·부탁). '~까지 해 주세요'는 '하겠습니다'가 섞여도 결정이 아니라 할 일로 본다
RECHECK_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
    "seq": {"type": "integer"}, "is_decision": {"type": "boolean"}, "statement": {"type": "string"},
    "topic": {"type": "string"}, "proposal_seq": {"type": "integer"}}, "required": ["seq", "is_decision"]}}},
    "required": ["items"]}

ASSIGN = re.compile(r"까지|주세요|부탁")

TOPIC_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"},
                "project": {"type": "string", "enum": list(PROJECTS)}}, "required": ["topic", "project"]}


def topic_key(raw: str | None) -> str:
    """
    모델이 돌려준 주제 문자열에서 주제 키만 추출

    모델이 주제 목록 줄을 통째로 돌려줘도('session.store: 대화 세션 저장소') 키만 남긴다. 키가 틀리면 원장 조회가 빈다.
    Args:
        raw: 모델이 고른 주제 문자열 (None 가능)
    Returns:
        주제 키. 키 형식이 아니면 빈 문자열
    Examples:
        topic_key('session.store: 대화 세션 저장소') -> 'session.store'
        topic_key('new:<영문 소문자>') -> ''
    """
    t = (raw or "").strip().removeprefix("new:").split(":")[0].strip()
    # 작은 모델은 지시문 예시('<영문 소문자>')를 그대로 따라 쓰기도 한다 → 키 형식이 아니면 버리고 검색 후보로 대신
    return t if re.fullmatch(r"[a-z][a-z0-9_.]*", t) else ""


async def ensure_topic(deps: "LedgerDeps", topic: str, text: str) -> str:
    """
    빈 주제 키를 임베딩 검색 1순위 주제로 보충

    LLM 선택이 실패했을 때의 대비책.
    Args:
        deps: 의존성 (mcp)
        topic: topic_key로 정리한 주제 키 (빈 문자열 가능)
        text: 검색에 쓸 문장
    Returns:
        주제 키. 검색 결과도 없으면 'unknown'
    """
    if topic:
        return topic
    cands = await deps.mcp.call("topic_candidates", text=text, k=1)
    return cands[0]["topic"] if cands else "unknown"


def _lines(segs: list[dict]) -> str:
    """
    발언 목록을 '[번호] 화자(역할): 내용' 줄로 직렬화
    Args:
        segs: 회의·면접 발언 목록
    Returns:
        줄바꿈으로 이은 전사문 문자열
    """
    return "\n".join(f"[{s['seq']}] {s['speaker']}({s['role']}): {s['text']}" for s in segs)


@traceable(name="plan_node")
async def plan_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    검토할 항목(items)을 만드는 계획 노드

    Wave 4 — 계획. MCP Prompt가 표준 절차를, MCP Resource가 주제 목록·용어집을 깔아준다.
    회의 정리면 결정 후보를 뽑아 '검토할 항목'으로 만든다. LLM은 발언 번호만 고르고 인용문은 쓰지 않는다.
    의견·질의는 메시지 1건을 항목으로 만들고 주제·프로젝트를 정한다. 면접은 plan_interview로 넘긴다.
    Args:
        state: 그래프 상태 (mode, message, meeting_id, scope_project 사용)
        deps: 의존성 (mcp, fast_llm, quality_llm)
    Returns:
        의견·질의: items(1건), usage, events(PlanCreated) 갱신
        회의: segments, meeting_date, attendee_roles, items, action_items, undecided, overview, usage, events 갱신
    """
    topics = await deps.mcp.resource("ledger://topics")
    if state["mode"] not in ("meeting", "interview"):
        projects = "\n".join(f"{k}: {v}" for k, v in PROJECTS.items())
        out, usage = await deps.fast_llm.json(
            "문장의 주제를 아래 목록의 키 하나로 고른다. 맞는 게 없으면 new:<영문 소문자>.\n" + topics
            + "\n\n문장이 어느 프로젝트 이야기인지도 고른다 (언급이 없으면 현재 프로젝트).\n" + projects,
            state["message"], TOPIC_SCHEMA)
        topic = await ensure_topic(deps, topic_key(out.get("topic")), state["message"])
        # 판단 기준 프로젝트: 사용자가 고른 것 > 질문에서 판단한 것 > 기본값(OPS). 어떻게 정했는지 화면에 남긴다
        if state.get("scope_project") in KEY_TO_PID:
            pid, how = KEY_TO_PID[state["scope_project"]], "직접 지정"
        elif out.get("project") and out["project"] != DEFAULT_PID:
            pid, how = out["project"], "질문에서 판단"
        else:
            pid, how = DEFAULT_PID, "기본값 — 질문에 프로젝트 언급 없음"
        item = {"statement": state["message"], "topic": topic, "project": pid}
        return {"items": [item], "usage": _usage(state, usage),
                "events": _ev(state, {"type": "PlanCreated", "items": 1, "topic": topic,
                                      "project": PID_TO_KEY.get(pid, pid), "how": how})}

    if state["mode"] == "interview":
        return await plan_interview(state, deps)
    segs = await deps.mcp.call("get_segments", meeting_id=state["meeting_id"])
    if not segs:
        return {"items": [], "segments": [],
                "events": _ev(state, {"type": "PlanCreated", "items": 0, "reason": "원문 없음(파기 또는 없음)"})}
    procedure = await deps.mcp.prompt("extract_decisions_prompt", meeting_id=state["meeting_id"])
    glossary = await deps.mcp.resource("project-doc://GLOSSARY/current")
    # 후보는 규칙으로 먼저 찾는다 — 확정 표현(결정), 기한·부탁 표현(할 일), 보류 표현(미결). LLM은 후보를 분류·요약한다
    # 배정 표현이 있으면 결정 후보에서 빼고 할 일 후보로만 표시 (gate와 같은 기준)
    marks = {}
    for sg in segs:
        tag = ("결정?" if is_decision_utterance(sg) and not ASSIGN.search(sg["text"]) else "") \
            + ("할일?" if ASSIGN.search(sg["text"]) else "") + ("미결?" if NOT_CONFIRM.search(sg["text"]) else "")
        if tag:
            marks[sg["seq"]] = tag
    if not marks:          # 결정·할 일·미결 후보가 없으면 LLM을 부르지 않는다
        return {"segments": segs, "meeting_date": segs[0]["date"], "attendee_roles": sorted({x["role"] for x in segs}),
                "items": [], "action_items": [], "undecided": [],
                "events": _ev(state, {"type": "PlanCreated", "items": 0, "action_items": 0, "reason": "후보 발언 없음"})}
    # 회의 내용 줄이기: 후보 발언과 그 앞 2발언(제안·맥락)만 보낸다. 나머지는 생략 표시
    keep = sorted({q for m in marks for q in (m - 2, m - 1, m)})
    by_seq = {x["seq"]: x for x in segs}
    lines, prev = [], None
    for q in keep:
        if q not in by_seq:
            continue
        # 번호가 끊기면 '…'로 생략을 표시 — 모델이 떨어진 발언을 이어진 대화로 읽지 않게
        if prev is not None and q != prev + 1:
            lines.append("  …")
        x = by_seq[q]
        lines.append(f"[{q}] {x['speaker']}({x['role']}): {x['text']}" + (f"   ← {marks[q]}" if q in marks else ""))
        prev = q
    sent = " ".join(by_seq[q]["text"] for q in keep if q in by_seq)
    # 용어집 표에서 보낸 발언에 실제로 나온 오인식(3번째 열 'STT 오인식 예')이 있는 줄만 넣는다. 머리글 줄은 제외
    gl_rows = [r for r in glossary.splitlines() if r.startswith("| ") and not r.startswith("| 표준")
               and any(w.strip() and w.strip() in sent for w in r.split("|")[3].split(","))]
    # 회의 요약용 전체 흐름: 모든 발언을 앞 40자만 (결정 추출은 아래 후보 발언 원문으로)
    outline = "\n".join(f"[{x['seq']}] {x['speaker']}: {x['text'][:40]}" for x in segs)
    user = (f"## 표준 절차\n{procedure}\n\n## 주제 목록 (없으면 new:<영문>)\n{topics}\n\n"
            f"## 회의 전체 흐름 (요약용, 발언 앞부분만)\n{outline}\n\n"
            + ("## 용어집 (이 회의에 나온 오인식)\n" + "\n".join(gl_rows) + "\n\n" if gl_rows else "")
            + f"## 회의 {state['meeting_id']} ({segs[0]['date']}) — 후보 발언과 앞 맥락만\n" + "\n".join(lines))
    out, usage = await deps.quality_llm.json(
        "후보 발언을 결정·할 일로 분류하고 요약한다.\n"
        "- decisions: 확정한 발언 seq, 그 결정의 제안 발언 proposal_seq, 결정 내용을 명사형 한 줄로(예: '실시간 전송을 WebSocket으로 변경'), topic.\n"
        "  statement는 원문을 베끼지 말고 '무엇을 어떻게'만 25자 이내로 쓴다 (예: '운영 로그 원문을 외부 LLM으로 비교').\n"
        "  STT 오인식(예: 버니게이트)은 용어집 표기(권위 게이트)로 고쳐 쓴다.\n"
        "- '그렇게 진행하죠'처럼 앞 제안을 받아들이는 발언도 결정이다. 할 일 배정·수락('네 하겠습니다')은 결정이 아니다.\n"
        "- action_items: 배정 발언 seq, 할 일 한 줄, owner(이름만), due(YYYY-MM-DD, 회의 연도). 없으면 null.\n"
        "  '누가 ~해 주세요'처럼 담당자 없는 요청도 할 일로 뽑고 owner는 null.\n"
        "- undecided: 논의했지만 확정하지 않은 주제.\n"
        "- overview: 전체 흐름을 보고 topic(회의 주제 한 줄), discussion(논의한 내용 3~5개, 각 40자 이내), "
        "direction(앞으로의 방향 1~2문장)을 쓴다. 결정 목록을 반복하지 말고 왜 그 얘기가 나왔는지를 담는다.", user, EXTRACT_SCHEMA)
    # 누락 재확인: 코드가 '결정?'으로 표시한 후보 중 모델이 어디에도 분류하지 않은 발언만 한 번 더 묻는다
    # (이미 분류된 결과는 건드리지 않아 다른 결정을 흔들지 않는다 — Wave 7 replan과 같은 '부족하면 한 번 더')
    classified = {x.get("seq") for k in ("decisions", "action_items", "undecided") for x in out.get(k, [])}
    missed = [q for q, tag in marks.items() if "결정?" in tag and q not in classified]
    recheck_usage = None
    if missed:
        ctx = "\n".join(f"[{x['seq']}] {x['speaker']}({x['role']}): {x['text']}"
                        for x in segs if any(m - 2 <= x["seq"] <= m for m in missed))
        rc, recheck_usage = await deps.quality_llm.json(
            "아래 표시된 발언이 회의에서 무언가를 정한 '결정'인지 하나씩 판정한다. 진행 안내·질문·회상·보류는 결정이 아니다. "
            "'그건 ~로 해요', '~로 걸러요'처럼 진행자가 방법을 단정하면 결정이다. 결정이면 statement(무엇을 어떻게, 25자 이내), "
            "topic(주제 목록 키), proposal_seq(그 결정의 제안 발언, 없으면 같은 번호)를 쓴다.\n\n## 주제 목록\n" + topics,
            f"## 판정할 발언 번호: {missed}\n\n## 앞 맥락 포함 발언\n{ctx}", RECHECK_SCHEMA, max_tokens=500)
        for r in rc.get("items", []):
            if r.get("is_decision") and r.get("seq") in missed and r.get("statement"):
                out.setdefault("decisions", []).append({
                    "seq": r["seq"], "proposal_seq": r.get("proposal_seq") or r["seq"],
                    "statement": r["statement"], "topic": r.get("topic") or ""})
    mpid = KEY_TO_PID.get(state["meeting_id"].split("-")[0], DEFAULT_PID)      # 회의 정리는 그 회의의 프로젝트가 기준
    items = [{**d, "decided_seq": d["seq"], "topic": await ensure_topic(deps, topic_key(d["topic"]), d["statement"]),
              "project": mpid} for d in out.get("decisions", [])]
    return {"segments": segs, "meeting_date": segs[0]["date"],
            "attendee_roles": sorted({s["role"] for s in segs}), "items": items,
            "action_items": out.get("action_items", []), "undecided": out.get("undecided", []),
            "overview": out.get("overview") or {}, "usage": _usage(state, usage, recheck_usage),
            "events": _ev(state, {"type": "PlanCreated", "items": len(items),
                                  "action_items": len(out.get("action_items", [])), "rechecked": len(missed)})}


# ---------------------------------------------------------------- 면접 전사문 (같은 그래프, plan·write만 도메인별)
COMP_SCHEMA = {"type": "object", "properties": {"qa": {"type": "array", "items": {"type": "object", "properties": {
    "question_seq": {"type": "integer"}, "question": {"type": "string"},
    "answer_seqs": {"type": "array", "items": {"type": "integer"}}, "answer": {"type": "string"}},
    "required": ["question_seq", "question", "answer_seqs", "answer"]}}},
    "required": ["qa"]}
# 공정성 점검 후보 — 면접관 발언에서 규칙으로 찾는다 (FAIR-01 금지 질문, FAIR-03 면접 중 합격 확정)
PRIVATE_Q = re.compile(r"결혼|임신|자녀|아이는|가족|부모|아버지|어머니|고향|출신|재산|몸무게|키가|종교|나이가")
PASS_CALL = re.compile(r"(합격|불합격)(으로|시키| 처리).*(하죠|합시다|하겠습니다)|붙이죠|떨어뜨리죠")


async def plan_interview(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    면접 전사문 계획 노드

    질문 요약·답변 요약 쌍은 LLM(발언 번호 포함), 공정성 점검 후보는 규칙.
    점수·합격 여부는 내지 않는다.
    Args:
        state: 그래프 상태 (meeting_id 사용)
        deps: 의존성 (mcp, quality_llm)
    Returns:
        segments, meeting_date, attendee_roles, items(공정성 점검 후보), competencies(질문·답변 쌍),
        action_items·undecided(빈 목록), usage, events(PlanCreated) 갱신
    """
    segs = await deps.mcp.call("get_segments", meeting_id=state["meeting_id"])
    if not segs:
        return {"items": [], "segments": [],
                "events": _ev(state, {"type": "PlanCreated", "items": 0, "reason": "원문 없음"})}
    procedure = await deps.mcp.prompt("summarize_interview_prompt", meeting_id=state["meeting_id"])
    out, usage = await deps.quality_llm.json(
        "면접관 질문마다 질문 요약(25자 이내)과 지원자 답변 요약(60자 이내)을 쌍으로 쓴다. "
        "question_seq는 면접관 질문 발언 번호, answer_seqs는 그 질문에 대한 지원자 답변 발언 번호. "
        "답변을 거부했으면 '답변 거부'로 쓴다. 평가·점수·합격 여부는 쓰지 않는다. 질문이 아닌 진행 발언은 뺀다.",
        f"## 표준 절차\n{procedure}\n\n## 면접 {state['meeting_id']}\n{_lines(segs)}", COMP_SCHEMA)
    items = []
    for sg in segs:
        # 공정성 점검은 면접관 발언만 대상 (지원자가 스스로 가족 얘기를 해도 위반이 아니다)
        if sg["role"] != "면접관":
            continue
        # 한 발언에 둘 다 걸리면 금지 질문(FAIR-01)으로만 올린다
        if PRIVATE_Q.search(sg["text"]):
            items.append({"statement": f"면접관 질문: {sg['text']}", "topic": "interview.question",
                          "decided_seq": sg["seq"], "rule_hint": "FAIR-01"})
        elif PASS_CALL.search(sg["text"]):
            items.append({"statement": f"면접관 발언: {sg['text']}", "topic": "interview.decision",
                          "decided_seq": sg["seq"], "rule_hint": "FAIR-03"})
    return {"segments": segs, "meeting_date": segs[0]["date"], "attendee_roles": sorted({x["role"] for x in segs}),
            "items": items, "competencies": out.get("qa", []), "action_items": [], "undecided": [],
            "usage": _usage(state, usage),
            "events": _ev(state, {"type": "PlanCreated", "items": len(items),
                                  "competencies": len(out.get("qa", [])), "domain": "interview"})}


# ---------------------------------------------------------------- gate
def _cite(by_seq: dict, seqs) -> list[dict]:
    """
    발언 번호 목록으로 원문 인용 목록 생성

    인용문은 모델 출력이 아니라 원문에서 그대로 채운다. 원문에 없는 번호는 버린다.
    Args:
        by_seq: 발언 번호 -> 발언 dict
        seqs: 인용할 발언 번호들 (중복·None 허용)
    Returns:
        번호순으로 정렬한 인용 목록 (meeting_id, seq, speaker, role, quote)
    """
    return [{"meeting_id": by_seq[q]["meeting_id"], "seq": q, "speaker": by_seq[q]["speaker"],
             "role": by_seq[q]["role"], "quote": by_seq[q]["text"]} for q in sorted(set(seqs)) if q in by_seq]


@traceable(name="gate_node")
async def gate_node(state: LedgerState, deps: LedgerDeps) -> dict:  # pylint: disable=unused-argument
    """
    모델이 뽑은 결정·할 일·미결을 코드 기준으로 거르는 권위 게이트

    Wave 6 — 권위 게이트. 모델이 뽑았어도 코드 기준에 안 맞으면 버린다.
    - 확정 발언(decided_seq)이 원문에 없거나, 확정 표현도 진행자(PM·PL) 발언도 아니면 결정이 아니다 (회상·제안 차단)
    - '아직 확정은 아닙니다' 같은 보류 표현, 할 일 배정 발언은 결정에서 뺀다
    - 인용문은 원문에서 코드가 채운다 → 인용 조작이 구조적으로 불가능
    할 일은 담당자·기한을 원문과 대조해 지어낸 값을 비운다. 의견·질의는 그대로 통과.
    Args:
        state: 그래프 상태 (mode, segments, items, competencies, action_items, undecided, meeting_date 사용)
        deps: 의존성 (이 노드에서는 쓰지 않음)
    Returns:
        회의: items(통과한 결정 + 인용), action_items, undecided, events(GateApplied, DecisionFound) 갱신
        면접: items(공정성 후보 + 인용), competencies(근거 검증), events(GateApplied, FairnessFlag) 갱신
        그 외: events(GateApplied)만 갱신
    """
    if state["mode"] == "interview":
        by_seq = {s["seq"]: s for s in state["segments"]}
        items = [{**i, "decided_by": by_seq[i["decided_seq"]]["speaker"], "decided_by_role": "면접관",
                  "evidence": _cite(by_seq, [i["decided_seq"]])} for i in state["items"]]
        comps, dropped = [], 0
        for c in state.get("competencies", []):
            qseg = by_seq.get(c.get("question_seq"))
            if not qseg or qseg["role"] != "면접관":                 # 질문은 면접관 발언이어야 한다
                dropped += 1
                continue
            ok = [q for q in c.get("answer_seqs", []) if q in by_seq and by_seq[q]["role"] == "지원자"]
            dropped += len(c.get("answer_seqs", [])) - len(ok)       # 답변 근거에 면접관 발언이 섞이면 버린다
            comps.append({**c, "q_evidence": _cite(by_seq, [c["question_seq"]]), "evidence": _cite(by_seq, ok)})
        evs = [{"type": "FairnessFlag", "seq": i["decided_seq"], "rule": i["rule_hint"]} for i in items]
        return {"items": items, "competencies": comps,
                "events": _ev(state, {"type": "GateApplied", "removed": dropped}, *evs)}
    if state["mode"] != "meeting":
        return {"events": _ev(state, {"type": "GateApplied", "removed": 0})}
    by_seq = {s["seq"]: s for s in state["segments"]}
    kept, removed, seen = [], [], set()
    for d in state["items"]:
        seg = by_seq.get(d.get("decided_seq"))
        # 버리는 조건: 원문에 없는 번호 / 결정 발언 아님 / 배정 발언 / 같은 발언을 두 결정으로 중복 추출
        if not seg or not is_decision_utterance(seg) or ASSIGN.search(seg["text"]) or d["decided_seq"] in seen:
            removed.append(d.get("decided_seq"))
            continue
        seen.add(d["decided_seq"])
        q, p = d["decided_seq"], d.get("proposal_seq")      # 근거 = 제안 발언(3발언 이내) + 확정 발언
        # 제안 번호가 확정 발언보다 뒤거나 너무 멀면 모델이 잘못 짚은 것으로 보고 확정 발언만 남긴다
        ev = ([p] if isinstance(p, int) and q - 3 <= p < q else []) + [q]
        kept.append({**d, "decided_by": seg["speaker"], "decided_by_role": seg["role"], "evidence": _cite(by_seq, ev)})
    year = state["meeting_date"][:4]
    actions = []
    for a in state.get("action_items", []):
        q = a.get("seq")
        nxt = by_seq.get(q + 1) if isinstance(q, int) else None
        prv = by_seq.get(q - 1) if isinstance(q, int) else None
        ow = a.get("owner") or ""
        # 바로 앞뒤 발언이 담당자 본인 것이면 근거에 넣는다 (자청 '제가 할게요' / 수락 '네 하겠습니다').
        # 화자 비교는 이름 앞 3자 — 모델이 이름 뒤에 직함·괄호를 붙여 써도 맞춘다
        ev = ([q - 1] if prv and ow and prv["speaker"].startswith(ow[:3]) else []) + [q] \
            + ([q + 1] if nxt and ow and nxt["speaker"].startswith(ow[:3]) else [])
        said = " ".join(by_seq[x]["text"] for x in ev if x in by_seq)
        due = a.get("due")
        # 모델이 null을 문자열로 쓰는 경우를 None으로 통일
        if isinstance(due, str) and due.strip().lower() in ("null", "none", "없음", ""):
            due = None
        if due and re.match(r"\d{4}-\d{2}-\d{2}$", due):
            due = year + due[4:]                      # 모델이 연도를 틀리는 경우 회의 연도로 고정
            if f"{int(due[8:])}일" not in said:      # 원문에 없는 기한은 지어낸 것 → 비운다
                due = None
        # 기한 정정: 배정 뒤 2발언 안에 진행자·담당자가 같은 일을 다른 날짜로 다시 말하면 나중 말이 기한
        # '같은 일'은 할 일 문장의 2자 이상 단어가 그 발언에 하나라도 나오는지로 판단
        words = re.findall(r"[가-힣A-Za-z]{2,}", a.get("statement") or "")
        for x in (q + 1, q + 2) if isinstance(q, int) else ():
            s = by_seq.get(x)
            if not s or not any(w in s["text"] for w in words):
                continue
            # 정정 권한: 진행자(PM·PL) 또는 담당자 본인만. 다른 참석자의 날짜 언급은 기한을 바꾸지 않는다
            if s["role"] not in CHAIR_ROLES and not (ow and s["speaker"].startswith(ow[:3])):
                continue
            # '11월 3일' 또는 '3일' — 월이 없으면 회의가 열린 달
            if m := re.search(r"(?:(\d{1,2})월\s*)?(\d{1,2})일", s["text"]):
                month = int(m.group(1) or state["meeting_date"][5:7])
                due = f"{year}-{month:02d}-{int(m.group(2)):02d}"
                ev = sorted({*ev, x})
        owner = re.sub(r"\s*\(.*\)", "", a["owner"]).strip() if a.get("owner") else None
        speakers = {by_seq[x]["speaker"] for x in ev if x in by_seq}
        # 이름 끝 2자로 원문을 찾는다 — 회의에서는 '민수 씨'처럼 성을 빼고 부르는 경우가 많다
        if owner and owner[-2:] not in said and owner not in speakers:
            owner = None                              # 원문에 이름도 없고 본인 발언도 아니면 지어낸 담당자
        req = by_seq.get(q) if isinstance(q, int) else None
        if owner and req and req["speaker"] == owner and re.search(r"주세요|부탁", req["text"]):
            owner = None                              # '누가 ~해 주세요'를 말한 사람은 요청자이지 담당자가 아니다
        actions.append({**a, "owner": owner, "due": due, "evidence": _cite(by_seq, ev)})
    undecided = [{**u, "evidence": _cite(by_seq, [u.get("seq")])} for u in state.get("undecided", [])]
    evs = [{"type": "DecisionFound", "statement": d["statement"], "seq": d["decided_seq"],
            "decided_by": d["decided_by"]} for d in kept]
    # 원문 근거가 하나도 없는 할 일·미결(존재하지 않는 번호)은 보고서에서 뺀다
    return {"items": kept, "action_items": [a for a in actions if a["evidence"]],
            "undecided": [u for u in undecided if u["evidence"]],
            "events": _ev(state, {"type": "GateApplied", "removed": len(removed), "removed_seq": removed}, *evs)}


# ---------------------------------------------------------------- research
@traceable(name="research_node")
async def research_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    항목마다 현재 유효한 기준·예외·권한을 조회하는 노드

    Wave 10b 병렬 + Wave 9 재시도(LedgerMCP.call) + Wave 2 Tool 호출.
    항목마다 '지금 유효한 기준'을 MCP Tool로 묻는다. R4 > R3 > R2 > R1 해석은 서버(resolve_current)가 한다.
    replan_check 뒤 다시 들어오면 이미 조사한 항목은 건너뛰고 R1 원문 조회만 추가한다.
    Args:
        state: 그래프 상태 (mode, items, meeting_date, attendee_roles, scope_meeting 사용)
        deps: 의존성 (mcp, today)
    Returns:
        items(research, waivers, authority_check, discussion 추가), events(TaskCompleted) 갱신
    """
    async def one(item: dict) -> dict:
        """
        항목 하나 조사
        Args:
            item: 검토 항목
        Returns:
            research·waivers·authority_check(재조사면 discussion)를 붙인 항목
        """
        if item.get("research"):                       # 이미 조사한 항목 — replan이 요청한 R1 조회만 추가
            if item.get("need_transcript") and "discussion" not in item:
                return {**item, "discussion": await deps.mcp.call("search_transcript", query=item["statement"], k=4,
                                                                  meeting_id=state.get("scope_meeting") or "")}
            return item
        res = await deps.mcp.call("resolve_current", text=item["statement"], topic=item["topic"],
                                  project_id=item.get("project") or "")
        # 면접 공정성 후보는 plan 규칙이 조항을 지목했으므로, 검색에서 빠졌어도 그 조항을 판정에 넣는다
        if item.get("rule_hint") and item["rule_hint"] not in {r["rule_id"] for r in res.get("rules", [])}:
            rule = json.loads(await deps.mcp.resource(f"policy://current/{item['rule_hint']}"))   # 규칙이 지목한 조항은 직접 가져온다
            res = {**res, "rules": [*res.get("rules", []), rule]}
        # 예외 유효 여부는 회의 날짜 기준 — 지난 회의는 그때 살아 있던 예외로 판단 (의견·질의는 오늘)
        on = state.get("meeting_date") or deps.today
        waivers = []
        for r in res.get("rules", []):
            waivers += (await deps.mcp.call("check_waiver", rule_id=r["rule_id"], on_date=on,
                                            project_id=item.get("project") or ""))["active_waivers"]
        auth = None
        # 권한 확인은 회의 결정만 — 누가 정했는지(decided_by_role)가 있어야 권한표와 대조할 수 있다
        if state["mode"] == "meeting":
            auth = await deps.mcp.call("check_authority", topic=AUTH_TOPIC.get(item["topic"], "tech.stack"),
                                       decided_by_role=item["decided_by_role"],
                                       attendee_roles=state["attendee_roles"])
        return {**item, "research": res, "waivers": waivers, "authority_check": auth}

    # 재조사 이벤트와 첫 조사 이벤트를 구분하려고 호출 전 상태를 기억한다
    before = [bool(i.get("research")) for i in state["items"]]
    # 조회 실패 항목은 failed 표시를 남긴다 — '기준 없음'과 섞이면 오류가 '문제없음'으로 조용히 바뀐다
    items = await gather_safe([one(i) for i in state["items"]],
                              lambda n, e: {**state["items"][n], "research": {"topic": state["items"][n]["topic"],
                                            "authority": "none", "failed": True, "notes": [f"조회 실패: {e!r}"]}})
    evs = [{"type": "TaskCompleted", "statement": i["statement"][:40], "source": "R1 원문 논의"}
           for i, b in zip(items, before) if b and i.get("discussion") is not None]
    evs += [{"type": "TaskCompleted", "statement": i["statement"][:40], "topic": i["topic"],
            "authority": i["research"]["authority"],
            "current": (i["research"].get("current") or {}).get("decision_id")} for i, b in zip(items, before) if not b]
    return {"items": list(items), "events": _ev(state, *evs)}


@traceable(name="replan_check_node")
async def replan_check_node(state: LedgerState, deps: LedgerDeps) -> dict:  # pylint: disable=unused-argument
    """
    수집 결과가 충분한지 확인하는 재계획 노드

    Wave 7 — 수집이 충분한가. 유효한 결정·기준 문서가 없는 항목은 회의 원문(R1) 논의를 추가 조회한다 (1회).
    Args:
        state: 그래프 상태 (items, replan_rounds 사용)
        deps: 의존성 (이 노드에서는 쓰지 않음)
    Returns:
        충분: events(ReplanCheck, is_sufficient=True)만 갱신
        부족: items(need_transcript 표시), replan_rounds(+1), events(ReplanCheck, is_sufficient=False) 갱신
    """
    rounds = state.get("replan_rounds", 0)
    # 이미 원문 조회를 요청한 항목은 다시 세지 않는다 (같은 공백으로 반복 진입 방지)
    gaps = [n for n, i in enumerate(state["items"])
            if i["research"]["authority"] in ("none", "discussion") and not i.get("need_transcript")]
    # 재조사는 1회 상한 — 원문에도 없으면 더 돌아도 결과가 같다
    if not gaps or rounds >= 1:
        return {"events": _ev(state, {"type": "ReplanCheck", "round": rounds, "is_sufficient": True})}
    items = [{**i, "need_transcript": True} if n in gaps else i for n, i in enumerate(state["items"])]
    return {"items": items, "replan_rounds": rounds + 1,
            "events": _ev(state, {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
                                  "gap": [items[n]["statement"][:30] for n in gaps]})}


def _after_replan(state: LedgerState) -> str:
    """
    replan_check 다음 노드 선택
    Args:
        state: 그래프 상태 (events 사용)
    Returns:
        'analysis'(충분) 또는 'research'(재조사)
    """
    # 판단 결과를 상태 키로 따로 두지 않고 replan_check가 마지막에 남긴 이벤트를 읽는다
    return "analysis" if state["events"][-1].get("is_sufficient") else "research"


# ---------------------------------------------------------------- analysis
JUDGE_ANSWER_SCHEMA = {"type": "object", "properties": {
    "relevance": {"type": "integer"}, "grounding": {"type": "integer"}, "completeness": {"type": "integer"},
    "clarity": {"type": "integer"}, "feedback": {"type": "string"}},
    "required": ["relevance", "grounding", "completeness", "clarity", "feedback"]}

JUDGE_SCHEMA = {"type": "object", "properties": {
    "checks": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string"}, "relation": {"type": "string", "enum": ["same", "different", "unrelated"]},
        "reason": {"type": "string"}}, "required": ["ref", "relation", "reason"]}}},
    "required": ["checks"]}

WAIVER_SCHEMA = {"type": "object", "properties": {"covers": {"type": "boolean"}, "reason": {"type": "string"}},
                 "required": ["covers", "reason"]}

JUDGE_SYSTEM = (
    "검토 대상이 아래 기준 각각과 어떤 관계인지 판정한다. 기준마다 한 번씩만 답한다.\n"
    "- same: 같은 방향이거나 기준을 지킨다 / different: 기준과 다르거나 기준을 어긴다 / unrelated: 상관없다.\n"
    "- 정책은 문장 그대로 적용한다 (예: 운영 로그 원문 외부 전송 금지 → 원문을 보내면 different).\n"
    "- reason은 20자 이내 한 구절.")


def _sources(item: dict) -> list[dict]:
    """
    판정에 넣을 '지금 유효한 기준' 목록 생성

    대체된 결정·구 정책은 resolve_current 단계에서 이미 빠져 있다.
    종류(kind): decision(현재 결정), baseline(기준 문서), policy(정책), pending(승인 대기), advisory(다른 프로젝트).
    Args:
        item: research 결과가 붙은 검토 항목
    Returns:
        기준 목록 (ref, kind, text)
    """
    res, out = item["research"], []
    if res["authority"] == "decision":
        c = res["current"]
        out.append({"ref": c["decision_id"], "kind": "decision",
                    "text": f"{c['statement']} (이유: {c.get('rationale')})"})
    for b in res.get("baseline", []):
        # 기준 문서는 절 번호(section 첫 단어)를 ref로 쓴다. 본문은 200자로 잘라 프롬프트 길이를 줄인다
        out.append({"ref": b["section"].split()[0], "kind": "baseline",
                    "text": f"{b['doc_id']} {b['doc_version']} {b['text'][:200]}"})
    for r in res.get("rules", []):
        if not r["rule_id"].startswith("AUTH"):          # 권한표는 check_authority가 코드로 따로 본다
            out.append({"ref": r["rule_id"], "kind": "policy", "text": r["text"]})
    for p in res.get("pending", []):
        out.append({"ref": p["decision_id"], "kind": "pending", "text": f"[승인 대기] {p['statement']}"})
    for a in res.get("advisory", []):
        out.append({"ref": a["decision_id"], "kind": "advisory",
                    "text": f"[다른 프로젝트] {a['statement']} (이유: {a.get('rationale')})"})
    return out


def decide(item: dict, checks: list[dict], waiver_covers: dict) -> dict:
    """
    LLM 관계 판정을 충돌 종류로 바꾸는 판정표 (코드)

    LLM이 고른 same/different를 충돌 종류로 바꾼다.
    정책 different              → policy 충돌. 범위가 맞는 유효 예외가 있으면 '예외 적용'
    권한표 위반(check_authority) → authority 충돌 (LLM 판단과 무관하게 코드가 추가)
    결정·기준 문서 different     → 권한자가 정했거나 범위가 맞는 예외가 있고, 다른 충돌이 없으면 '기준 변경(승인됨)', 아니면 충돌
    다른 프로젝트 different      → 참고 (충돌 아님)
    승인 대기 결정 same          → 그 결정을 확정하는 발언 (원장 승인 대상)
    Args:
        item: research 결과가 붙은 검토 항목 (waivers, authority_check 포함)
        checks: LLM 판정 목록 (ref, relation, reason)
        waiver_covers: 예외 ID -> 예외 범위 안이면 True
    Returns:
        review dict (status: ok/conflict/change_approved, conflicts, changes, confirms, waiver_applied, notes)
    """
    srcs = {s["ref"]: s for s in _sources(item)}
    rel = {}
    for c in checks:
        if c.get("ref") in srcs and c["ref"] not in rel:      # 없는 ID·중복 답변은 버린다
            rel[c["ref"]] = c
    conflicts = []
    # 예외 범위가 맞으면(LLM covers) 그 예외를 적용 — 모델이 정책을 same으로 봤어도 '예외 범위 안'임을 표시
    waiver_applied = next((w["waiver_id"] for w in item.get("waivers", [])
                           if w["rule_id"] in srcs and waiver_covers.get(w["waiver_id"])), None)
    for ref, c in rel.items():
        if c["relation"] != "different" or srcs[ref]["kind"] != "policy":
            continue
        # 예외는 그 정책 조항(rule_id)에 걸린 것만 충돌을 면제한다
        w = next((w for w in item.get("waivers", [])
                  if w["rule_id"] == ref and waiver_covers.get(w["waiver_id"])), None)
        if not w:
            conflicts.append({"type": "policy", "against": [ref], "detail": c["reason"]})
    auth = item.get("authority_check")
    # 명시적 False만 위반. 권한 규칙이 없는 주제는 ok=True·rule=None으로 와서 위반도 권한자 결정도 아님
    if auth and auth.get("ok") is False:
        conflicts.append({"type": "authority", "against": [auth["rule"].split("@")[0]],
                          "detail": f"승인 권한자 {auth['approver']} — {auth['reason']}"})
    # 권한표 조항(rule)을 근거로 통과했을 때만 '권한자 결정'으로 인정
    authorized = bool(auth and auth.get("ok") and auth.get("rule")) or bool(waiver_applied)  # 예외 승인도 승인
    changes = []
    confirms = [ref for ref, c in rel.items() if srcs[ref]["kind"] == "pending" and c["relation"] == "same"]
    for ref, c in rel.items():
        kind = srcs[ref]["kind"]
        if c["relation"] != "different" or kind in ("policy", "pending"):
            continue
        if kind == "advisory":
            conflicts.append({"type": "past_project", "against": [ref], "detail": c["reason"]})
        # 정책·권한 충돌이 하나라도 있으면 권한자 결정이어도 기준 변경으로 인정하지 않는다
        elif authorized and not [x for x in conflicts if x["type"] != "past_project"]:
            changes.append(ref)
        else:
            conflicts.append({"type": "past_decision" if kind == "decision" else "baseline",
                              "against": [ref], "detail": c["reason"]})
    # 다른 프로젝트 참고(past_project)는 표시만 하고 상태를 conflict로 만들지 않는다
    blocking = [c for c in conflicts if c["type"] != "past_project"]
    status = "conflict" if blocking else ("change_approved" if changes else "ok")
    return {"status": status, "conflicts": conflicts, "changes": changes, "confirms": confirms,
            "waiver_applied": waiver_applied,
            "notes": item["research"].get("notes", [])}


@traceable(name="analysis_node")
async def analysis_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    항목별 충돌 판정 노드

    LLM은 기준마다 같은지·다른지만 고르고, 충돌 종류는 코드 판정표(decide)가 정한다.
    질의는 판정 없이 통과. MCP Prompt(conflict_review_prompt)의 절차를 판정표로 코드화했다.
    Args:
        state: 그래프 상태 (mode, items 사용)
        deps: 의존성 (quality_llm: 관계 판정, fast_llm: 예외 범위 판단)
    Returns:
        items(review 추가), usage, events(ConflictChecked) 갱신. 질의·항목 없음이면 events(AnalysisSkipped)만
    """
    if state["mode"] == "question" or not state.get("items"):
        return {"events": _ev(state, {"type": "AnalysisSkipped"})}
    async def judge(item):
        """
        항목 하나의 기준 관계와 예외 범위를 판정
        Args:
            item: research 결과가 붙은 검토 항목
        Returns:
            (review를 붙인 항목, 사용량 dict 또는 None)
        """
        if item.get("research", {}).get("failed"):   # 기준을 못 읽었으면 판정하지 않고 사람에게 돌린다
            return {**item, "review": {"status": "needs_approval", "conflicts": [], "changes": [],
                                       "waiver_applied": None,
                                       "notes": ["기준 조회 실패 — 사람 검토 필요"] + item["research"]["notes"]}}, None
        srcs = _sources(item)
        if not srcs:                              # 비교할 기준이 없으면 LLM을 부르지 않는다
            return {**item, "review": decide(item, [], {})}, None
        lines = "\n".join(f"- {s['ref']} ({s['kind']}): {s['text']}" for s in srcs)
        said = "\n".join(f"- {e['speaker']}: {e['quote']}" for e in item.get("evidence", []))
        user = (f"## 검토 대상\n{item['statement']}\n" + (f"원문 발언:\n{said}\n" if said else "")
                + f"\n## 기준\n{lines}")
        out, usage = await deps.quality_llm.json(JUDGE_SYSTEM, user, JUDGE_SCHEMA, max_tokens=600)
        covers = {}
        for w in item.get("waivers", []):          # 예외 범위는 따로 한 문항으로 묻는다 (섞어 물으면 놓친다)
            ans, u = await deps.fast_llm.json(
                "검토 대상이 예외 승인 범위 안에 들어가는지 판단한다. 데이터 종류(마스킹 여부·원문)와 용도가 같고 "
                "수량이 범위 이하면 covers=true. 원문 데이터이거나 용도가 다르면 false. "
                "'그대로', '실제 운영 로그', '원문'은 마스킹하지 않은 원문이다.",
                f"## 예외 범위\n{w['scope']}\n\n## 검토 대상\n{item['statement']}\n{said}", WAIVER_SCHEMA)
            raw = re.search(r"그대로|원문|실제 운영 로그|실제 로그", item["statement"] + " " + said)
            # 코드 가드: 예외 범위가 '마스킹' 한정인데 원문 표현이 있으면 모델 판단과 상관없이 예외 불가
            covers[w["waiver_id"]] = bool(ans.get("covers")) and not ("마스킹" in w["scope"] and raw)
            # 추가 호출 사용량을 판정 호출 usage에 합쳐 _usage가 한 번에 세게 한다
            usage = {**usage, "tokens": usage.get("tokens", 0) + u.get("tokens", 0),
                     "extra_calls": usage.get("extra_calls", 0) + 1}
        return {**item, "review": decide(item, out.get("checks", []), covers)}, usage

    # 판정이 실패한 항목은 '문제없음'으로 넘기지 않고 '승인 필요'로 사람에게 돌린다
    results = await gather_safe([judge(i) for i in state["items"]],
                                lambda n, e: ({**state["items"][n], "review": {
                                               "status": "needs_approval", "conflicts": [],
                                               "changes": [], "waiver_applied": None,
                                               "notes": [f"판정 실패 — 사람 검토 필요: {e!r}"]}}, None))
    items = [r[0] for r in results]
    evs = [{"type": "ConflictChecked", "statement": i["statement"][:40], "status": i["review"]["status"],
            "conflicts": [f"{c['type']}:{','.join(c['against'])}" for c in i["review"]["conflicts"]]}
           for i in items]
    return {"items": items, "usage": _usage(state, *[r[1] for r in results]), "events": _ev(state, *evs)}


# ---------------------------------------------------------------- write / validate
STATUS_KO = {"ok": "문제없음", "conflict": "충돌", "change_approved": "기준 변경(승인됨, 문서 개정 필요)",
             "needs_approval": "승인 필요"}


def render_meeting(state: LedgerState) -> str:
    """
    회의 정리 보고서를 템플릿으로 작성

    숫자·ID·인용이 모델을 거치지 않도록 구조화 결과를 그대로 마크다운으로 옮긴다.
    Args:
        state: 그래프 상태 (meeting_id, meeting_date, overview, items, action_items, undecided 사용)
    Returns:
        회의 요약·결정·할 일·미결 논의 마크다운
    """
    rows = [f"## {state['meeting_id']} 회의 정리 ({state.get('meeting_date', '')})", ""]
    ov = state.get("overview") or {}
    if ov:
        rows += ["### 회의 요약", f"주제: {ov.get('topic', '')}"]
        rows += [f"- {d}" for d in ov.get("discussion", [])]
        if ov.get("direction"):
            rows.append(f"방향: {ov['direction']}")
        rows.append("")
    rows.append("### 결정")
    for n, d in enumerate(state["items"], 1):
        r = d["review"]
        rows.append(f"{n}) **{d['statement']}** — {STATUS_KO.get(r['status'], r['status'])}  (결정 {d['decided_by']})")
        rows.append("   근거: " + "; ".join(f"[{e['seq']}] {e['speaker']} \"{e['quote']}\"" for e in d["evidence"]))
        for c in r["conflicts"]:
            rows.append(f"   - {c['type']} [{', '.join(c['against'])}] {c['detail']}")
        if r.get("changes"):
            rows.append(f"   - 기준 변경: {', '.join(r['changes'])} (권한자 승인 — 문서 개정 필요)")
        if r.get("confirms"):
            rows.append(f"   - 승인 대기 결정 {', '.join(r['confirms'])}과 같은 내용 — 원장에서 승인하면 확정")
        if r.get("waiver_applied"):
            rows.append(f"   - 예외 적용: {r['waiver_applied']}")
    rows += ["", "### 할 일"]
    for a in state.get("action_items", []):
        # gate가 비운 담당자·기한은 '?'와 경고로 드러낸다 (빈칸을 지어내지 않음)
        miss = [k for k in ("owner", "due") if not a.get(k)]
        warn = f"  ⚠ {'·'.join(miss)} 없음" if miss else ""
        rows.append(f"- {a['statement']} — 담당 {a.get('owner') or '?'} / 기한 {a.get('due') or '?'}{warn} "
                 f"(근거 {', '.join(str(e['seq']) for e in a['evidence'])})")
    if state.get("undecided"):
        rows += ["", "### 미결 논의"]
        rows += [f"- {u['subject']} (근거 {', '.join(str(e['seq']) for e in u['evidence'])})" for u in state["undecided"]]
    return "\n".join(rows)


def render_interview(state: LedgerState) -> str:
    """
    면접 요약 보고서를 템플릿으로 작성

    질문·답변 요약 쌍과 공정성 점검 결과만 쓴다. 평가·합격 여부는 쓰지 않는다.
    Args:
        state: 그래프 상태 (meeting_id, meeting_date, competencies, items 사용)
    Returns:
        질문·답변 요약과 공정성 점검 마크다운
    """
    rows = [f"## {state['meeting_id']} 면접 요약 ({state.get('meeting_date', '')})", "", "### 질문·답변 요약"]
    for n, c in enumerate(state.get("competencies", []), 1):
        aseq = ", ".join(str(e["seq"]) for e in c["evidence"]) or "—"
        rows += [f"{n}) **Q** {c['question']}  [{c['question_seq']}]", f"   **A** {c['answer']}  [{aseq}]"]
    rows += ["", "### 공정성 점검"]
    if not state.get("items"):
        rows.append("- 문제 발언 없음")
    for i in state.get("items", []):
        r = i["review"]
        e = i["evidence"][0]
        mark = "⚠" if r["status"] == "conflict" else "✓"
        rows.append(f"- {mark} [{e['seq']}] {e['speaker']} \"{e['quote']}\"")
        rows += [f"  - {c['type']} [{', '.join(c['against'])}] {c['detail']}" for c in r["conflicts"]]
    return "\n".join(rows)


def render_opinion(item: dict) -> str:
    """
    의견 검토 결과를 템플릿으로 작성
    Args:
        item: review가 붙은 의견 항목
    Returns:
        검토 결과·충돌·예외·참고 마크다운
    """
    r = item["review"]
    blocking = [c for c in r["conflicts"] if c["type"] != "past_project"]
    # 예외가 적용되고 막는 충돌이 없으면 '문제없음' 대신 '예외 범위 안에서 가능'으로 조건을 드러낸다
    verdict = ("예외 범위 안에서 가능" if r.get("waiver_applied") and not blocking
               else STATUS_KO.get(r["status"], r["status"]))
    rows = [f"**검토 결과: {verdict}** (주제 {item['topic']})"]
    rows += [f"- {c['type']} [{', '.join(c['against'])}] {c['detail']}" for c in r["conflicts"]]
    if r.get("waiver_applied"):
        rows.append(f"- 적용 가능한 예외: {r['waiver_applied']}")
    rows += [f"- 참고: {n}" for n in r.get("notes", [])]
    return "\n".join(rows)


def _bullets(text: str | None) -> list[str]:
    """
    쉼표·가운뎃점·세미콜론으로 이어 쓴 문장을 항목 목록으로 분리
    Args:
        text: 원장의 rationale 같은 문자열 (None 가능)
    Returns:
        공백을 정리한 항목 목록 (빈 항목 제외)
    """
    return [t.strip() for t in re.split(r",\s*|·|;", text or "") if t.strip()]


async def _question_sources(item: dict, deps: LedgerDeps, user_id: str = "",
                            restricted: list | None = None) -> tuple[str, str]:
    """
    질의 답변용 근거 생성

    질의 근거를 두 가지로 만든다: LLM에 줄 근거 글, 답변 아래에 코드가 그대로 붙일 근거 블록(원장 데이터 그대로).
    유효 결정 → 원장의 근거 발언을 원문에서 다시 꺼낸다 (원문 파기면 원장 인용).
    Args:
        item: research 결과가 붙은 질의 항목
        deps: 의존성 (mcp, can_view)
        user_id: 열람 권한 확인에 쓸 사용자 ID. 비어 있으면 권한 확인 생략
        restricted: 열람 권한이 없어 가린 결정 ID를 담을 리스트 (호출자가 넘기면 여기에 추가)
    Returns:
        (LLM용 근거 글, 답변 아래 붙일 근거 블록)
    """
    res, rows, blocks = item["research"], [f"권위 출처: {item['research']['authority']}"], []
    cur_id = res["current"]["decision_id"] if res["authority"] == "decision" else None
    pend = {p["decision_id"] for p in res.get("pending", [])}
    decs = ([res["current"]] if cur_id else []) + res.get("pending", []) + res.get("advisory", [])
    asked = item.get("project", DEFAULT_PID)
    if asked != DEFAULT_PID:      # 다른 프로젝트를 물으면 그 프로젝트 결정을 먼저 (나머지는 참고로 뒤에)
        decs = [d for d in decs if d["project_id"] == asked] + [d for d in decs if d["project_id"] != asked]
        rows.append(f"질문 대상: {PROJECTS.get(asked, asked)}")
    for d in decs:
        full = await deps.mcp.call("get_decision", decision_id=d["decision_id"])
        did = full["decision_id"]
        # 권한 확인은 LLM 근거 글을 만들기 전에 — 내용이 프롬프트에 들어가면 답변으로 새어 나갈 수 있다
        if deps.can_view and user_id and not deps.can_view(user_id, full):
            rows.append(f"[{did}] 열람 권한 없음 — 내용 비공개 (열람 요청 필요)")
            blocks.append(f"### {did} · 열람 권한 없음\n열람 요청이 승인되면 내용을 볼 수 있습니다.")
            if restricted is not None:
                restricted.append(did)
            continue
        pname = PROJECTS.get(full["project_id"], full["project_id"])
        rows.append(f"[{did}] 상태 {full['status']} · 프로젝트 {pname} · 시행 {full.get('effective_from')} · 결정 "
                 f"{full.get('decided_by')}: {full['statement']} / 이유: {full.get('rationale')} "
                 f"/ 기각한 대안: {', '.join(full.get('alternatives_rejected') or [])}")
        # 표시 꼬리표 우선순위: 승인 대기 > 현재 유효 > 질문한 프로젝트 > 다른 프로젝트 참고
        tag = ("승인 대기 · 아직 권위 없음" if did in pend else "현재 유효" if did == cur_id
               else "질문한 프로젝트" if full["project_id"] == asked else f"다른 프로젝트 참고 · {pname.split('(')[0]}")
        blk = [f"### {did} · {full['statement'].rstrip('.')}",
               " · ".join(x for x in (tag, full.get("effective_from") and f"시행 {full['effective_from']}",
                                     full.get("decided_by") and f"결정 {full['decided_by']}") if x)]
        if _bullets(full.get("rationale")):
            blk += ["", "이유"] + [f"- {r}" for r in _bullets(full.get("rationale"))]
        if full.get("alternatives_rejected"):
            blk += ["", "기각한 대안"] + [f"- {x}" for x in full["alternatives_rejected"]]
        for e in full.get("evidence", []):
            seg = (await deps.mcp.call("get_segments", meeting_id=e["meeting_id"], seq_from=e["seq"], seq_to=e["seq"])
                   if e.get("seq") else [])
            # 원문이 남아 있으면 원문을, 보존 기한이 지나 파기됐으면 원장에 저장된 인용을 쓴다
            if seg:
                rows.append(f"  원문 {e['meeting_id']} [{e.get('seq')}]: \"{seg[0]['text']}\"")
                blk += ["", f"원문 · {e['meeting_id']} [{e['seq']}] {seg[0]['speaker']}", f"> {seg[0]['text']}"]
            else:
                rows.append(f"  원장 인용(원문 파기) {e['meeting_id']}: \"{e['quote']}\"")
                blk += ["", f"원장 인용 · {e['meeting_id']} (원문은 보존 기한이 지나 파기)", f"> {e['quote']}"]
        blocks.append("\n".join(blk))
    if res["authority"] == "baseline":
        b = res["current"]
        rows.append(f"[기준 문서] {b['doc_id']} {b['doc_version']} {b['section']}: {b['text']}")
        blocks.append(f"### 기준 문서 · {b['doc_id']} {b['doc_version']} {b['section']}\n> {b['text'][:300]}")
    if item.get("discussion"):
        blocks.append("### 원문 논의 · 결정되지 않음\n" + "\n".join(
            f"- {x['meeting_id']} [{x['seq']}] {x['speaker']}: {x['text']}" for x in item["discussion"]))
    for x in item.get("discussion", []):
        rows.append(f"[원문 논의·미결] {x['meeting_id']} [{x['seq']}] {x['speaker']}: \"{x['text']}\"")
    return "\n".join(rows), "\n\n".join(blocks)


ANSWER_SYSTEM = (
    "프로젝트 결정 이력 담당자로서 질문에 1~2문장으로 직접 답한다. 이유·기각한 대안·원문 인용은 답변 아래에 따로 붙으니 반복하지 않는다.\n"
    "결정이 있으면 결정 ID를 한 번 밝힌다. 승인 대기면 '아직 승인 대기', 논의만 있으면 '논의는 있었지만 결정되지 않음'이라고 쓴다. "
    "질문이 특정 프로젝트(예: 고객센터 챗봇)를 묻으면 그 프로젝트의 결정으로 답한다. "
    "'열람 권한 없음'인 결정은 ID만 말하고 내용을 추측하지 않는다. 근거에 없는 내용과 날짜는 쓰지 않는다.")


@traceable(name="write_node")
async def write_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    모드별 답변 초안 작성 노드

    Wave 8 / 10a — 작성. 회의·의견은 구조화 결과를 템플릿으로(숫자·ID가 바뀌지 않게),
    질의 답변만 고품질 모델이 토큰 스트리밍으로 쓴다. 반려되면 피드백을 받아 다시 쓴다.
    Args:
        state: 그래프 상태 (mode, items, segments, message, validation_feedback 등)
        deps: 의존성 (quality_llm, mcp)
    Returns:
        draft, usage, events(DraftWritten) 갱신. 질의면 restricted(가린 결정 ID)도 갱신
    """
    attempt = state.get("validation_attempts", 0) + 1
    usage = None
    if state["mode"] in ("meeting", "interview") and not state.get("segments"):
        draft = f"{state['meeting_id']} 원문이 없습니다 (파기됐거나 없음)."
    elif state["mode"] == "meeting":
        draft = render_meeting(state)
    elif state["mode"] == "interview":
        draft = render_interview(state)
    elif state["mode"] == "opinion":
        draft = render_opinion(state["items"][0])
    else:
        restricted: list[str] = []
        sources, blocks = await _question_sources(state["items"][0], deps, state.get("user_id", ""), restricted)
        # 재작성 때는 validate가 남긴 반려 사유를 프롬프트에 붙여 같은 실수를 고치게 한다
        fb = f"\n\n## 이전 답변 반려 사유\n{state['validation_feedback']}" if state.get("validation_feedback") else ""
        writer = get_stream_writer()
        draft, usage = await deps.quality_llm.text(
            ANSWER_SYSTEM, f"## 질문\n{state['message']}\n\n## 근거\n{sources}{fb}",
            on_token=lambda p: writer({"type": "AnswerToken", "attempt": attempt, "delta": p}), max_tokens=220)
        draft = draft.strip() + ("\n\n" + blocks if blocks else "")     # 근거 블록은 원장 데이터를 코드가 그대로 붙임
    out = {"draft": draft, "usage": _usage(state, usage),
           "events": _ev(state, {"type": "DraftWritten", "attempt": attempt, "length": len(draft)})}
    if state["mode"] == "question":
        out["restricted"] = restricted
        out["answer_sources"] = sources        # 품질 심판이 답과 근거를 대조하는 데 쓴다
    return out


@traceable(name="validate_node")
async def validate_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    초안을 코드 기준으로 검증하는 노드

    Wave 8 — 코드 기준 검증 (모델에게 '맞냐'고 묻지 않는다). 최대 3회.
    회의: 인용이 원문과 글자까지 같은가 / 질의: 결정 ID, '승인 대기', '결정되지 않음'을 밝혔는가.
    Args:
        state: 그래프 상태 (mode, draft, items, competencies, segments, validation_attempts 사용)
        deps: 의존성 (이 노드에서는 쓰지 않음)
    Returns:
        validation_attempts, validation_feedback(통과면 None), events(Validation) 갱신.
        질의가 3회째에도 불합격이면 draft에 빠진 사실을 덧붙여 갱신
    """
    attempts = state.get("validation_attempts", 0) + 1
    problems = []
    judge = judge_usage = None
    if state["mode"] in ("meeting", "interview"):
        by_seq = {s["seq"]: s["text"] for s in state.get("segments", [])}
        for d in state.get("items", []) + state.get("competencies", []):
            no_evidence = not d["evidence"] and "decided_seq" in d          # 결정은 근거 필수 (역량 요약은 '근거 없음' 허용)
            if no_evidence or any(by_seq.get(e["seq"]) != e["quote"] for e in d["evidence"]):
                problems.append(f"인용 불일치: {d.get('statement') or d.get('question')}")
    elif state["mode"] == "question":
        item = state["items"][0]
        res = item["research"]
        # 답변에 반드시 나와야 할 결정 ID: 현재 유효 결정 + 승인 대기 결정
        ids = ([res["current"]["decision_id"]] if res["authority"] == "decision" else []) + \
              [p["decision_id"] for p in res.get("pending", [])]
        # 다른 프로젝트를 물었으면 그 프로젝트의 결정 ID가 기준 (없으면 위 목록 유지)
        if item.get("project", DEFAULT_PID) != DEFAULT_PID:
            ids = [a["decision_id"] for a in res.get("advisory", []) if a["project_id"] == item["project"]] or ids
        if ids and not any(i in state["draft"] for i in ids):
            problems.append(f"근거 결정 ID({', '.join(ids)})를 답변에 밝히세요.")
        if res.get("pending") and "승인 대기" not in state["draft"]:
            problems.append("승인 대기 결정이라는 점을 밝히세요.")
        if not ids and res["authority"] in ("none", "discussion") and "결정되지 않" not in state["draft"]:
            problems.append("유효한 결정이 없으니 '결정되지 않음'을 밝히세요.")
        # 코드 검사를 통과했으면 LLM 심판(LLM-as-Judge)이 질문 대비 답의 품질을 4기준으로 채점 — 3점 미만이면 재작성
        if not problems and attempts < 3:
            short = state["draft"].split("\n\n### ")[0]          # 코드가 붙인 근거 블록은 빼고 모델이 쓴 답만
            sc, judge_usage = await deps.fast_llm.json(
                "질문에 대한 답을 근거와 비교해 1~5점으로 채점한다. "
                "relevance(질문에 바로 답하는가), grounding(근거에 있는 내용만 쓰는가), "
                "completeness(결정 번호·상태 등 필요한 사실을 빠뜨리지 않는가), clarity(짧고 분명한가). "
                "3점 미만인 기준이 있으면 feedback에 고칠 점 한 줄.",
                f"## 질문\n{state['message']}\n\n## 근거\n{state.get('answer_sources', '')[:2500]}\n\n## 답\n{short}",
                JUDGE_ANSWER_SCHEMA, max_tokens=200)
            scores = {k: sc.get(k) for k in ("relevance", "grounding", "completeness", "clarity")
                      if isinstance(sc.get(k), int)}
            judge = {"scores": scores, "feedback": sc.get("feedback", "")}
            if scores and min(scores.values()) < 3:
                problems.append(f"품질 심판: {sc.get('feedback') or '점수 미달'}")
    # 3회째에는 문제가 남아도 통과시킨다 — write ⇄ validate 무한 반복 방지
    ok = not problems or attempts >= 3
    verdict = "합격" if not problems else ("합격(최대 시도 도달)" if ok else "불합격")
    ev = {"type": "Validation", "attempt": attempts, "verdict": verdict, "problems": problems}
    if judge:
        ev["judge"] = judge
    out = {"validation_attempts": attempts, "validation_feedback": None if ok else "\n".join(problems),
           "events": _ev(state, ev)}
    if judge_usage:
        out["usage"] = _usage(state, judge_usage)
    if problems and ok and state["mode"] == "question":
        # 3번 다시 써도 빠진 사실은 코드가 덧붙인다 (모델에게 맡기지 않는 마지막 안전장치)
        res = state["items"][0]["research"]
        notes = []
        if res["authority"] == "decision" and res["current"]["decision_id"] not in state["draft"]:
            notes.append(f"근거 결정: {res['current']['decision_id']}")
        notes += [f"{p['decision_id']}는 아직 승인 대기 (권위 없음)" for p in res.get("pending", [])
                  if "승인 대기" not in state["draft"]]
        if not notes and res["authority"] in ("none", "discussion") and "결정되지 않" not in state["draft"]:
            notes.append("유효한 결정 없음 — 논의는 있었지만 결정되지 않음")
        if notes:
            out["draft"] = state["draft"] + "\n\n" + "\n".join(f"※ {n}" for n in notes)
    return out


def _after_validate(state: LedgerState) -> str:
    """
    validate 다음 노드 선택
    Args:
        state: 그래프 상태 (validation_feedback 사용)
    Returns:
        'finalize'(통과) 또는 'write'(반려 사유가 있어 재작성)
    """
    return "finalize" if state.get("validation_feedback") is None else "write"


@traceable(name="finalize_node")
async def finalize_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """
    최종 답변과 보고서를 확정하는 노드

    옵션이 켜져 있으면 문제없는 결정을 '승인 대기'로 원장에 올린다 (승인은 사람만).
    Args:
        state: 그래프 상태 (mode, draft, items, overview, action_items, undecided, restricted 사용)
        deps: 의존성 (mcp, propose_to_ledger)
    Returns:
        final(최종 답변), report(구조화 결과·원장 제안 ID), events(Finalize) 갱신
    """
    proposed = []
    if deps.propose_to_ledger and state["mode"] == "meeting":
        for d in state["items"]:
            if d["review"].get("confirms"):          # 기존 승인 대기 결정을 확정하는 발언 → 새로 올리지 않고 그 결정을 승인 대상으로
                proposed += d["review"]["confirms"]
            # 충돌·승인 필요 결정은 원장에 올리지 않는다
            elif d["review"]["status"] in ("ok", "change_approved"):
                r = await deps.mcp.call("propose_decision", statement=d["statement"], topic=d["topic"],
                                        decided_by=d["decided_by"],
                                        evidence=[{k: e[k] for k in ("meeting_id", "seq", "quote")}
                                                  for e in d["evidence"]])
                proposed.append(r.get("proposed"))
    report = {"mode": state["mode"], "items": state.get("items", []), "overview": state.get("overview", {}),
              "action_items": state.get("action_items", []), "undecided": state.get("undecided", []),
              "proposed": proposed, "restricted": state.get("restricted", [])}
    return {"final": state["draft"], "report": report,
            "events": _ev(state, {"type": "Finalize", "pipeline": state["mode"], "proposed": proposed})}


def _after_plan(state: LedgerState) -> str:
    """
    plan 다음 노드 선택

    원문이 없는 회의·면접(파기됐거나 잘못된 ID)은 조사 없이 바로 안내문 작성으로.
    Args:
        state: 그래프 상태 (mode, segments 사용)
    Returns:
        'write'(원문 없음) 또는 'gate'
    """
    if state["mode"] in ("meeting", "interview") and not state.get("segments"):
        return "write"
    return "gate"


def _after_route(state: LedgerState) -> str:
    """
    route 다음 노드 선택
    Args:
        state: 그래프 상태 (mode 사용)
    Returns:
        'chat_answer', 'schedule_answer', 'schedule_add', 또는 'plan'(meeting·interview·opinion·question)
    """
    return {"chat": "chat_answer", "schedule": "schedule_answer",
            "schedule_add": "schedule_add"}.get(state["mode"], "plan")   # interview도 plan 경로


def build_ledger_graph(deps: LedgerDeps):
    """
    결정 원장 LangGraph 그래프 조립·컴파일

    노드 함수에 deps를 partial로 묶어 등록하고 조건부 간선으로 루프(research ⇄ replan_check, write ⇄ validate)를 만든다.
    Args:
        deps: 모든 노드가 공유할 의존성
    Returns:
        컴파일된 그래프 (ainvoke·astream으로 실행)
    """
    g = StateGraph(LedgerState)

    def node(fn):
        return partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입

    for name, fn in [("route", route_node), ("chat_answer", chat_answer_node),
                     ("schedule_answer", schedule_answer_node), ("schedule_add", schedule_add_node), ("plan", plan_node),
                     ("gate", gate_node), ("research", research_node), ("replan_check", replan_check_node),
                     ("analysis", analysis_node), ("write", write_node), ("validate", validate_node),
                     ("finalize", finalize_node)]:
        g.add_node(name, node(fn))
    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "schedule_answer": "schedule_answer",
                             "schedule_add": "schedule_add", "plan": "plan"})
    g.add_edge("chat_answer", END)
    g.add_edge("schedule_answer", END)
    g.add_edge("schedule_add", END)
    g.add_conditional_edges("plan", _after_plan, {"gate": "gate", "write": "write"})
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan, {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate, {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
