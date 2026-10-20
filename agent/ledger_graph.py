"""결정 원장 Agent 그래프 — 베이스(all_total_vue의 agent/graph.py)와 같은 노드 이름·순서를 쓴다.

route ─┬ chat ─────────────────────────────────────────────── chat_answer
       └ meeting / opinion / question
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
from pathlib import Path
from functools import partial
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

TODAY = "2026-10-20"
_PRJ_FILE = Path(__file__).resolve().parent.parent / "data" / "project" / "projects.json"
# 프로젝트 이름표 (질문 대상 프로젝트 고르기·표시용). 약자·이름은 projects.json 한 곳에서 관리
PROJECTS = {p["project_id"]: f"{p['name']}({'현재' if p['status'] == '진행' else '과거'} 프로젝트, {p['key']})"
            for p in json.loads(_PRJ_FILE.read_text())["projects"] if p["key"] != "HBE"}
_PRJ_ALL = json.loads(_PRJ_FILE.read_text())["projects"]
KEY_TO_PID = {p["key"]: p["project_id"] for p in _PRJ_ALL}
PID_TO_KEY = {v: k for k, v in KEY_TO_PID.items()}
DEFAULT_PID = "PRJ-OPSAI"
MEETING_ID = re.compile(r"\b(?:([A-Z]{3})-)?([MI])(\d{2})\b")   # OPS-M04, HBE-I01, 또는 줄여서 M04·I01


def normalize_meeting_id(msg: str) -> str:
    """'M04'처럼 약자를 빼고 써도 받는다 (회의는 OPS, 면접은 HBE가 기본)."""
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
    if NOT_CONFIRM.search(seg["text"]):
        return False
    return bool(CONFIRM.search(seg["text"])) or seg["role"] in CHAIR_ROLES

# 주제 → 권한표(R4 AUTH-*) 주제. 목록에 없으면 기술 선택(tech.stack)
AUTH_TOPIC = {"schedule": "schedule", "budget": "budget", "perf.latency": "scope", "scope.core": "scope",
              "answer.grounding": "scope", "llm.external": "security", "meeting.retention": "security"}


@dataclass
class LedgerDeps:
    """Wave 13 — 의존성 주입. 노드는 이것만 보고 동작한다 (테스트에서 가짜로 갈아 끼움)."""
    fast_llm: Any              # Wave 10a — 분류·주제 선택
    quality_llm: Any           # Wave 10a — 추출·충돌 판정·답변 작성
    mcp: Any                   # Wave 2  — 결정 원장 MCP 서버
    session_store: Any = None  # Wave 12
    can_view: Any = None       # (user_id, decision) -> bool. 권한 없는 결정은 내용 자체를 LLM에 넘기지 않는다
    today: str = TODAY
    propose_to_ledger: bool = False


class LedgerState(TypedDict, total=False):
    message: str
    user_id: str
    scope_project: str        # 입력창 아래에서 사용자가 고른 프로젝트 약자 (없으면 자동)
    scope_meeting: str        # 입력창 아래에서 고른 회의 (없으면 지정 안 함)
    history: list[dict]
    restricted: list[str]     # 열람 권한이 없어 가린 결정 ID
    mode: str                 # meeting / interview / opinion / question / chat
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
    return state.get("events", []) + list(evs)


async def gather_safe(coros, fallback, limit: int = 2):
    """병렬 실행 + 에러 격리 — 한 건이 실패해도 나머지 결과로 보고서를 만든다 (실패분은 fallback).
    limit: 로컬 LLM 동시 호출 상한. 무제한 gather는 Ollama 대기열만 길어진다."""
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c
    results = await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
    return [fallback(i, r) if isinstance(r, Exception) else r for i, r in enumerate(results)]


def _usage(state, *us) -> dict:
    u = dict(state.get("usage") or {"llm_calls": 0, "tokens": 0})
    for x in us:
        if x:
            u["llm_calls"] += 1 + x.get("extra_calls", 0)
            u["tokens"] += x.get("tokens", 0)
    return u


# ---------------------------------------------------------------- route
SCHEDULE_Q = re.compile(r"(이번|지난|지지난|저번|저저번|다음|다다음)\s*주|전전?주|\d+\s*주\s*(전|후|뒤)|오늘|내일|어제|이번\s*달|지난\s*달|다음\s*달|\d{1,2}월|몇\s*(개|건|번)|언제\s*있")

ROUTE_SCHEMA = {"type": "object", "properties": {"mode": {"type": "string",
                "enum": ["meeting", "opinion", "question", "chat"]}}, "required": ["mode"]}


@traceable(name="route_node")
async def route_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 5 — 쿼리 라우팅. 분명한 경우는 규칙으로, 애매하면 저비용 모델이 고른다."""
    msg = state["message"]
    mid = normalize_meeting_id(msg) or (state.get("scope_meeting") or "")
    usage = None
    if SCHEDULE_Q.search(msg) and re.search(r"회의|면접|일정|미팅", msg) and not re.search(r"정리|요약|추출", msg):
        mode = "schedule"
    elif mid and re.search(r"정리|요약|추출|처리|분석", msg):
        mode = "interview" if "-I" in mid else "meeting"
    elif re.search(r"어떨까|어때|바꾸면|바꾸자|하자|쓰면|쓰자|제안|넣자", msg):
        mode = "opinion"
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
    """질문 속 기간 표현을 날짜 구간으로. 주는 월~일 기준."""
    from datetime import date, timedelta
    t = date.fromisoformat(today)
    mon = t - timedelta(days=t.weekday())
    # 주 단위: '지지난주·저저번주·전전주' = 2주 전, '다다음 주' = 2주 뒤, 'N주 전/후'는 숫자대로 (월~일 기준)
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
        a = b = t; label = "오늘"
    elif "내일" in msg:
        a = b = t + timedelta(days=1); label = "내일"
    elif "어제" in msg:
        a = b = t - timedelta(days=1); label = "어제"
    elif m := re.search(r"(\d{1,2})월", msg):
        mm = int(m.group(1)); a = date(t.year, mm, 1)
        b = (date(t.year + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)); label = f"{mm}월"
    elif re.search(r"(이번|지난|다음)\s*달", msg):
        a = t.replace(day=1); b = (a.replace(month=a.month % 12 + 1) - timedelta(days=1)); label = "이번 달"
    else:
        a, b, label = mon, mon + timedelta(days=6), "이번 주"
    return a.isoformat(), b.isoformat(), label


@traceable(name="schedule_answer_node")
async def schedule_answer_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """일정 질문은 LLM 없이 회의 목록(MCP Tool)으로 코드가 답한다 — 지어낼 여지가 없다."""
    a, b, label = _range(state["message"], deps.today)
    ms = [m for m in await deps.mcp.call("list_meetings") if a <= m["date"] <= b]
    q = state["message"]
    if re.search(r"면접", q):
        ms, kind = [m for m in ms if m.get("domain") == "interview"], "면접은"
    elif re.search(r"일정", q):
        kind = "일정은"            # 일정 = 회의 + 면접 전부
    else:
        ms, kind = [m for m in ms if m.get("domain") != "interview"], "회의는"
    days = "월화수목금토일"
    from datetime import date
    n_int = sum(m.get("domain") == "interview" for m in ms)
    detail = f" (회의 {len(ms) - n_int}건, 면접 {n_int}건)" if kind == "일정은" and ms else ""
    span = f"{a[5:]}~{b[5:]}" if a != b else a[5:]
    head = f"{label}({span}) {kind} {len(ms)}건입니다{detail}."
    lines = [head] + [f"- {m['meeting_id']} · {m['date'][5:]}({days[date.fromisoformat(m['date']).weekday()]}) {m['title']}"
                      f" · 참석 {', '.join(x for x in m['attendees'] if not x.startswith('지원자'))}" for m in sorted(ms, key=lambda x: x["date"])]
    return {"final": "\n".join(lines),
            "events": _ev(state, {"type": "ScheduleAnswered", "range": f"{a}~{b}", "count": len(ms)},
                          {"type": "Finalize", "pipeline": "schedule"})}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 5 direct 경로 — 세션 히스토리 기반 짧은 응답 (Wave 12 세션)."""
    hist = "\n".join(f"{h['role']}: {h['content']}" for h in (state.get("history") or [])[-6:])
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.text(
        "너는 회의 요약·결정 정리 비서다. 한두 문장으로 답한다. 할 수 있는 일: 회의록 정리('OPS-M04 회의 정리해줘'), "
        "제안 검토('Redis로 바꾸면 어때?'), 과거 결정 근거 질의('SSE는 왜 골랐어?'), 일정 질문('이번 주 회의 몇 개야?'). "
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

ASSIGN = re.compile(r"까지|주세요|부탁")

TOPIC_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"},
                "project": {"type": "string", "enum": list(PROJECTS)}}, "required": ["topic", "project"]}


def _lines(segs: list[dict]) -> str:
    return "\n".join(f"[{s['seq']}] {s['speaker']}({s['role']}): {s['text']}" for s in segs)


@traceable(name="plan_node")
async def plan_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 4 — 계획. MCP Prompt가 표준 절차를, MCP Resource가 주제 목록·용어집을 깔아준다.
    회의 정리면 결정 후보를 뽑아 '검토할 항목'으로 만든다. LLM은 발언 번호만 고르고 인용문은 쓰지 않는다."""
    topics = await deps.mcp.resource("ledger://topics")
    if state["mode"] not in ("meeting", "interview"):
        projects = "\n".join(f"{k}: {v}" for k, v in PROJECTS.items())
        out, usage = await deps.fast_llm.json(
            "문장의 주제를 아래 목록의 키 하나로 고른다. 맞는 게 없으면 new:<영문 소문자>.\n" + topics
            + "\n\n문장이 어느 프로젝트 이야기인지도 고른다 (언급이 없으면 현재 프로젝트).\n" + projects,
            state["message"], TOPIC_SCHEMA)
        topic = (out.get("topic") or "new:unknown").removeprefix("new:")
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
        if prev is not None and q != prev + 1:
            lines.append("  …")
        x = by_seq[q]
        lines.append(f"[{q}] {x['speaker']}({x['role']}): {x['text']}" + (f"   ← {marks[q]}" if q in marks else ""))
        prev = q
    sent = " ".join(by_seq[q]["text"] for q in keep if q in by_seq)
    gl_rows = [r for r in glossary.splitlines() if r.startswith("| ") and not r.startswith("| 표준")
               and any(w.strip() and w.strip() in sent for w in r.split("|")[3].split(","))]
    # 회의 요약용 전체 흐름: 모든 발언을 앞 40자만 (결정 추출은 아래 후보 발언 원문으로)
    outline = "\n".join(f"[{x['seq']}] {x['speaker']}: {x['text'][:40]}" for x in segs)
    user = (f"## 표준 절차\n{procedure}\n\n## 주제 목록 (없으면 new:<영문>)\n{topics}\n\n"
            f"## 회의 전체 흐름 (요약용, 발언 앞부분만)\n{outline}\n\n"
            + (f"## 용어집 (이 회의에 나온 오인식)\n" + "\n".join(gl_rows) + "\n\n" if gl_rows else "")
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
    mpid = KEY_TO_PID.get(state["meeting_id"].split("-")[0], DEFAULT_PID)      # 회의 정리는 그 회의의 프로젝트가 기준
    items = [{**d, "decided_seq": d["seq"], "topic": d["topic"].removeprefix("new:"), "project": mpid}
             for d in out.get("decisions", [])]
    return {"segments": segs, "meeting_date": segs[0]["date"],
            "attendee_roles": sorted({s["role"] for s in segs}), "items": items,
            "action_items": out.get("action_items", []), "undecided": out.get("undecided", []),
            "overview": out.get("overview") or {}, "usage": _usage(state, usage),
            "events": _ev(state, {"type": "PlanCreated", "items": len(items),
                                  "action_items": len(out.get("action_items", []))})}


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
    """면접 전사문 정리: 질문 요약·답변 요약 쌍은 LLM(발언 번호 포함), 공정성 점검 후보는 규칙.
    점수·합격 여부는 내지 않는다."""
    segs = await deps.mcp.call("get_segments", meeting_id=state["meeting_id"])
    if not segs:
        return {"items": [], "segments": [], "events": _ev(state, {"type": "PlanCreated", "items": 0, "reason": "원문 없음"})}
    procedure = await deps.mcp.prompt("summarize_interview_prompt", meeting_id=state["meeting_id"])
    out, usage = await deps.quality_llm.json(
        "면접관 질문마다 질문 요약(25자 이내)과 지원자 답변 요약(60자 이내)을 쌍으로 쓴다. "
        "question_seq는 면접관 질문 발언 번호, answer_seqs는 그 질문에 대한 지원자 답변 발언 번호. "
        "답변을 거부했으면 '답변 거부'로 쓴다. 평가·점수·합격 여부는 쓰지 않는다. 질문이 아닌 진행 발언은 뺀다.",
        f"## 표준 절차\n{procedure}\n\n## 면접 {state['meeting_id']}\n{_lines(segs)}", COMP_SCHEMA)
    items = []
    for sg in segs:
        if sg["role"] != "면접관":
            continue
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
    return [{"meeting_id": by_seq[q]["meeting_id"], "seq": q, "speaker": by_seq[q]["speaker"],
             "role": by_seq[q]["role"], "quote": by_seq[q]["text"]} for q in sorted(set(seqs)) if q in by_seq]


@traceable(name="gate_node")
async def gate_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 6 — 권위 게이트. 모델이 뽑았어도 코드 기준에 안 맞으면 버린다.
    - 확정 발언(decided_seq)이 원문에 없거나, 확정 표현도 진행자(PM·PL) 발언도 아니면 결정이 아니다 (회상·제안 차단)
    - '아직 확정은 아닙니다' 같은 보류 표현, 할 일 배정 발언은 결정에서 뺀다
    - 인용문은 원문에서 코드가 채운다 → 인용 조작이 구조적으로 불가능"""
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
        if not seg or not is_decision_utterance(seg) or ASSIGN.search(seg["text"]) or d["decided_seq"] in seen:
            removed.append(d.get("decided_seq"))
            continue
        seen.add(d["decided_seq"])
        q, p = d["decided_seq"], d.get("proposal_seq")      # 근거 = 제안 발언(3발언 이내) + 확정 발언
        ev = ([p] if isinstance(p, int) and q - 3 <= p < q else []) + [q]
        kept.append({**d, "decided_by": seg["speaker"], "decided_by_role": seg["role"], "evidence": _cite(by_seq, ev)})
    year = state["meeting_date"][:4]
    actions = []
    for a in state.get("action_items", []):
        q = a.get("seq")
        nxt = by_seq.get(q + 1) if isinstance(q, int) else None
        prv = by_seq.get(q - 1) if isinstance(q, int) else None
        ow = a.get("owner") or ""
        ev = ([q - 1] if prv and ow and prv["speaker"].startswith(ow[:3]) else []) + [q] \
            + ([q + 1] if nxt and ow and nxt["speaker"].startswith(ow[:3]) else [])
        said = " ".join(by_seq[x]["text"] for x in ev if x in by_seq)
        due = a.get("due")
        if isinstance(due, str) and due.strip().lower() in ("null", "none", "없음", ""):
            due = None
        if due and re.match(r"\d{4}-\d{2}-\d{2}$", due):
            due = year + due[4:]                      # 모델이 연도를 틀리는 경우 회의 연도로 고정
            if f"{int(due[8:])}일" not in said:      # 원문에 없는 기한은 지어낸 것 → 비운다
                due = None
        # 기한 정정: 배정 뒤 2발언 안에 진행자·담당자가 같은 일을 다른 날짜로 다시 말하면 나중 말이 기한
        words = re.findall(r"[가-힣A-Za-z]{2,}", a.get("statement") or "")
        for x in (q + 1, q + 2) if isinstance(q, int) else ():
            s = by_seq.get(x)
            if not s or not any(w in s["text"] for w in words):
                continue
            if s["role"] not in CHAIR_ROLES and not (ow and s["speaker"].startswith(ow[:3])):
                continue
            if m := re.search(r"(?:(\d{1,2})월\s*)?(\d{1,2})일", s["text"]):
                month = int(m.group(1) or state["meeting_date"][5:7])
                due = f"{year}-{month:02d}-{int(m.group(2)):02d}"
                ev = sorted({*ev, x})
        owner = re.sub(r"\s*\(.*\)", "", a["owner"]).strip() if a.get("owner") else None
        speakers = {by_seq[x]["speaker"] for x in ev if x in by_seq}
        if owner and owner[-2:] not in said and owner not in speakers:
            owner = None                              # 원문에 이름도 없고 본인 발언도 아니면 지어낸 담당자
        actions.append({**a, "owner": owner, "due": due, "evidence": _cite(by_seq, ev)})
    undecided = [{**u, "evidence": _cite(by_seq, [u.get("seq")])} for u in state.get("undecided", [])]
    evs = [{"type": "DecisionFound", "statement": d["statement"], "seq": d["decided_seq"],
            "decided_by": d["decided_by"]} for d in kept]
    return {"items": kept, "action_items": [a for a in actions if a["evidence"]],
            "undecided": [u for u in undecided if u["evidence"]],
            "events": _ev(state, {"type": "GateApplied", "removed": len(removed), "removed_seq": removed}, *evs)}


# ---------------------------------------------------------------- research
@traceable(name="research_node")
async def research_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 10b 병렬 + Wave 9 재시도(LedgerMCP.call) + Wave 2 Tool 호출.
    항목마다 '지금 유효한 기준'을 MCP Tool로 묻는다. R4 > R3 > R2 > R1 해석은 서버(resolve_current)가 한다."""
    async def one(item: dict) -> dict:
        if item.get("research"):                       # 이미 조사한 항목 — replan이 요청한 R1 조회만 추가
            if item.get("need_transcript") and "discussion" not in item:
                return {**item, "discussion": await deps.mcp.call("search_transcript", query=item["statement"], k=4,
                                                                  meeting_id=state.get("scope_meeting") or "")}
            return item
        res = await deps.mcp.call("resolve_current", text=item["statement"], topic=item["topic"],
                                  project_id=item.get("project") or "")
        if item.get("rule_hint") and item["rule_hint"] not in {r["rule_id"] for r in res.get("rules", [])}:
            rule = json.loads(await deps.mcp.resource(f"policy://current/{item['rule_hint']}"))   # 규칙이 지목한 조항은 직접 가져온다
            res = {**res, "rules": [*res.get("rules", []), rule]}
        on = state.get("meeting_date") or deps.today
        waivers = []
        for r in res.get("rules", []):
            waivers += (await deps.mcp.call("check_waiver", rule_id=r["rule_id"], on_date=on,
                                            project_id=item.get("project") or ""))["active_waivers"]
        auth = None
        if state["mode"] == "meeting":
            auth = await deps.mcp.call("check_authority", topic=AUTH_TOPIC.get(item["topic"], "tech.stack"),
                                       decided_by_role=item["decided_by_role"],
                                       attendee_roles=state["attendee_roles"])
        return {**item, "research": res, "waivers": waivers, "authority_check": auth}

    before = [bool(i.get("research")) for i in state["items"]]
    items = await gather_safe([one(i) for i in state["items"]],
                              lambda n, e: {**state["items"][n], "research": {"topic": state["items"][n]["topic"],
                                            "authority": "none", "notes": [f"조회 실패: {e!r}"]}})
    evs = [{"type": "TaskCompleted", "statement": i["statement"][:40], "source": "R1 원문 논의"}
           for i, b in zip(items, before) if b and i.get("discussion") is not None]
    evs += [{"type": "TaskCompleted", "statement": i["statement"][:40], "topic": i["topic"],
            "authority": i["research"]["authority"],
            "current": (i["research"].get("current") or {}).get("decision_id")} for i, b in zip(items, before) if not b]
    return {"items": list(items), "events": _ev(state, *evs)}


@traceable(name="replan_check_node")
async def replan_check_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 7 — 수집이 충분한가. 유효한 결정·기준 문서가 없는 항목은 회의 원문(R1) 논의를 추가 조회한다 (1회)."""
    rounds = state.get("replan_rounds", 0)
    gaps = [n for n, i in enumerate(state["items"])
            if i["research"]["authority"] in ("none", "discussion") and not i.get("need_transcript")]
    if not gaps or rounds >= 1:
        return {"events": _ev(state, {"type": "ReplanCheck", "round": rounds, "is_sufficient": True})}
    items = [{**i, "need_transcript": True} if n in gaps else i for n, i in enumerate(state["items"])]
    return {"items": items, "replan_rounds": rounds + 1,
            "events": _ev(state, {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
                                  "gap": [items[n]["statement"][:30] for n in gaps]})}


def _after_replan(state: LedgerState) -> str:
    return "analysis" if state["events"][-1].get("is_sufficient") else "research"


# ---------------------------------------------------------------- analysis
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
    """판정에 넣을 '지금 유효한 기준' 목록 — 대체된 결정·구 정책은 resolve_current 단계에서 이미 빠져 있다."""
    res, out = item["research"], []
    if res["authority"] == "decision":
        c = res["current"]
        out.append({"ref": c["decision_id"], "kind": "decision", "text": f"{c['statement']} (이유: {c.get('rationale')})"})
    for b in res.get("baseline", []):
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
    """판정표 (코드) — LLM이 고른 same/different를 충돌 종류로 바꾼다.
    정책 different              → policy 충돌. 범위가 맞는 유효 예외가 있으면 '예외 적용'
    권한표 위반(check_authority) → authority 충돌 (LLM 판단과 무관하게 코드가 추가)
    결정·기준 문서 different     → 권한자가 정했거나 범위가 맞는 예외가 있고, 다른 충돌이 없으면 '기준 변경(승인됨)', 아니면 충돌
    다른 프로젝트 different      → 참고 (충돌 아님)
    승인 대기 결정 same          → 그 결정을 확정하는 발언 (원장 승인 대상)"""
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
        w = next((w for w in item.get("waivers", [])
                  if w["rule_id"] == ref and waiver_covers.get(w["waiver_id"])), None)
        if not w:
            conflicts.append({"type": "policy", "against": [ref], "detail": c["reason"]})
    auth = item.get("authority_check")
    if auth and auth.get("ok") is False:
        conflicts.append({"type": "authority", "against": [auth["rule"].split("@")[0]],
                          "detail": f"승인 권한자 {auth['approver']} — {auth['reason']}"})
    authorized = bool(auth and auth.get("ok") and auth.get("rule")) or bool(waiver_applied)  # 예외 승인도 승인
    changes = []
    confirms = [ref for ref, c in rel.items() if srcs[ref]["kind"] == "pending" and c["relation"] == "same"]
    for ref, c in rel.items():
        kind = srcs[ref]["kind"]
        if c["relation"] != "different" or kind in ("policy", "pending"):
            continue
        if kind == "advisory":
            conflicts.append({"type": "past_project", "against": [ref], "detail": c["reason"]})
        elif authorized and not [x for x in conflicts if x["type"] != "past_project"]:
            changes.append(ref)
        else:
            conflicts.append({"type": "past_decision" if kind == "decision" else "baseline",
                              "against": [ref], "detail": c["reason"]})
    blocking = [c for c in conflicts if c["type"] != "past_project"]
    status = "conflict" if blocking else ("change_approved" if changes else "ok")
    return {"status": status, "conflicts": conflicts, "changes": changes, "confirms": confirms,
            "waiver_applied": waiver_applied,
            "notes": item["research"].get("notes", [])}


@traceable(name="analysis_node")
async def analysis_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """충돌 판정. LLM은 기준마다 같은지·다른지만 고르고, 충돌 종류는 코드 판정표(decide)가 정한다.
    질의는 판정 없이 통과. MCP Prompt(conflict_review_prompt)의 절차를 판정표로 코드화했다."""
    if state["mode"] == "question" or not state.get("items"):
        return {"events": _ev(state, {"type": "AnalysisSkipped"})}
    async def judge(item):
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
            usage = {**usage, "tokens": usage.get("tokens", 0) + u.get("tokens", 0),
                     "extra_calls": usage.get("extra_calls", 0) + 1}
        return {**item, "review": decide(item, out.get("checks", []), covers)}, usage

    results = await gather_safe([judge(i) for i in state["items"]],
                                lambda n, e: ({**state["items"][n], "review": {"status": "needs_approval", "conflicts": [],
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
    L = [f"## {state['meeting_id']} 회의 정리 ({state.get('meeting_date', '')})", ""]
    ov = state.get("overview") or {}
    if ov:
        L += ["### 회의 요약", f"주제: {ov.get('topic', '')}"]
        L += [f"- {d}" for d in ov.get("discussion", [])]
        if ov.get("direction"):
            L.append(f"방향: {ov['direction']}")
        L.append("")
    L.append("### 결정")
    for n, d in enumerate(state["items"], 1):
        r = d["review"]
        L.append(f"{n}) **{d['statement']}** — {STATUS_KO.get(r['status'], r['status'])}  (결정 {d['decided_by']})")
        L.append("   근거: " + "; ".join(f"[{e['seq']}] {e['speaker']} \"{e['quote']}\"" for e in d["evidence"]))
        for c in r["conflicts"]:
            L.append(f"   - {c['type']} [{', '.join(c['against'])}] {c['detail']}")
        if r.get("changes"):
            L.append(f"   - 기준 변경: {', '.join(r['changes'])} (권한자 승인 — 문서 개정 필요)")
        if r.get("confirms"):
            L.append(f"   - 승인 대기 결정 {', '.join(r['confirms'])}과 같은 내용 — 원장에서 승인하면 확정")
        if r.get("waiver_applied"):
            L.append(f"   - 예외 적용: {r['waiver_applied']}")
    L += ["", "### 할 일"]
    for a in state.get("action_items", []):
        miss = [k for k in ("owner", "due") if not a.get(k)]
        warn = f"  ⚠ {'·'.join(miss)} 없음" if miss else ""
        L.append(f"- {a['statement']} — 담당 {a.get('owner') or '?'} / 기한 {a.get('due') or '?'}{warn} "
                 f"(근거 {', '.join(str(e['seq']) for e in a['evidence'])})")
    if state.get("undecided"):
        L += ["", "### 미결 논의"]
        L += [f"- {u['subject']} (근거 {', '.join(str(e['seq']) for e in u['evidence'])})" for u in state["undecided"]]
    return "\n".join(L)


def render_interview(state: LedgerState) -> str:
    L = [f"## {state['meeting_id']} 면접 요약 ({state.get('meeting_date', '')})", "", "### 질문·답변 요약"]
    for n, c in enumerate(state.get("competencies", []), 1):
        aseq = ", ".join(str(e["seq"]) for e in c["evidence"]) or "—"
        L += [f"{n}) **Q** {c['question']}  [{c['question_seq']}]", f"   **A** {c['answer']}  [{aseq}]"]
    L += ["", "### 공정성 점검"]
    if not state.get("items"):
        L.append("- 문제 발언 없음")
    for i in state.get("items", []):
        r = i["review"]
        e = i["evidence"][0]
        mark = "⚠" if r["status"] == "conflict" else "✓"
        L.append(f"- {mark} [{e['seq']}] {e['speaker']} \"{e['quote']}\"")
        L += [f"  - {c['type']} [{', '.join(c['against'])}] {c['detail']}" for c in r["conflicts"]]
    return "\n".join(L)


def render_opinion(item: dict) -> str:
    r = item["review"]
    blocking = [c for c in r["conflicts"] if c["type"] != "past_project"]
    verdict = ("예외 범위 안에서 가능" if r.get("waiver_applied") and not blocking
               else STATUS_KO.get(r["status"], r["status"]))
    L = [f"**검토 결과: {verdict}** (주제 {item['topic']})"]
    L += [f"- {c['type']} [{', '.join(c['against'])}] {c['detail']}" for c in r["conflicts"]]
    if r.get("waiver_applied"):
        L.append(f"- 적용 가능한 예외: {r['waiver_applied']}")
    L += [f"- 참고: {n}" for n in r.get("notes", [])]
    return "\n".join(L)


def _bullets(text: str | None) -> list[str]:
    return [t.strip() for t in re.split(r",\s*|·|;", text or "") if t.strip()]


async def _question_sources(item: dict, deps: LedgerDeps, user_id: str = "",
                            restricted: list | None = None) -> tuple[str, str]:
    """질의 근거를 두 가지로 만든다: LLM에 줄 근거 글, 답변 아래에 코드가 그대로 붙일 근거 블록(원장 데이터 그대로).
    유효 결정 → 원장의 근거 발언을 원문에서 다시 꺼낸다 (원문 파기면 원장 인용)."""
    res, L, B = item["research"], [f"권위 출처: {item['research']['authority']}"], []
    cur_id = res["current"]["decision_id"] if res["authority"] == "decision" else None
    pend = {p["decision_id"] for p in res.get("pending", [])}
    decs = ([res["current"]] if cur_id else []) + res.get("pending", []) + res.get("advisory", [])
    asked = item.get("project", DEFAULT_PID)
    if asked != DEFAULT_PID:      # 다른 프로젝트를 물으면 그 프로젝트 결정을 먼저 (나머지는 참고로 뒤에)
        decs = [d for d in decs if d["project_id"] == asked] + [d for d in decs if d["project_id"] != asked]
        L.append(f"질문 대상: {PROJECTS.get(asked, asked)}")
    for d in decs:
        full = await deps.mcp.call("get_decision", decision_id=d["decision_id"])
        did = full["decision_id"]
        if deps.can_view and user_id and not deps.can_view(user_id, full):
            L.append(f"[{did}] 열람 권한 없음 — 내용 비공개 (열람 요청 필요)")
            B.append(f"### {did} · 열람 권한 없음\n열람 요청이 승인되면 내용을 볼 수 있습니다.")
            if restricted is not None:
                restricted.append(did)
            continue
        pname = PROJECTS.get(full["project_id"], full["project_id"])
        L.append(f"[{did}] 상태 {full['status']} · 프로젝트 {pname} · 시행 {full.get('effective_from')} · 결정 "
                 f"{full.get('decided_by')}: {full['statement']} / 이유: {full.get('rationale')} "
                 f"/ 기각한 대안: {', '.join(full.get('alternatives_rejected') or [])}")
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
            if seg:
                L.append(f"  원문 {e['meeting_id']} [{e.get('seq')}]: \"{seg[0]['text']}\"")
                blk += ["", f"원문 · {e['meeting_id']} [{e['seq']}] {seg[0]['speaker']}", f"> {seg[0]['text']}"]
            else:
                L.append(f"  원장 인용(원문 파기) {e['meeting_id']}: \"{e['quote']}\"")
                blk += ["", f"원장 인용 · {e['meeting_id']} (원문은 보존 기한이 지나 파기)", f"> {e['quote']}"]
        B.append("\n".join(blk))
    if res["authority"] == "baseline":
        b = res["current"]
        L.append(f"[기준 문서] {b['doc_id']} {b['doc_version']} {b['section']}: {b['text']}")
        B.append(f"### 기준 문서 · {b['doc_id']} {b['doc_version']} {b['section']}\n> {b['text'][:300]}")
    if item.get("discussion"):
        B.append("### 원문 논의 · 결정되지 않음\n" + "\n".join(
            f"- {x['meeting_id']} [{x['seq']}] {x['speaker']}: {x['text']}" for x in item["discussion"]))
    for x in item.get("discussion", []):
        L.append(f"[원문 논의·미결] {x['meeting_id']} [{x['seq']}] {x['speaker']}: \"{x['text']}\"")
    return "\n".join(L), "\n\n".join(B)


ANSWER_SYSTEM = (
    "프로젝트 결정 이력 담당자로서 질문에 1~2문장으로 직접 답한다. 이유·기각한 대안·원문 인용은 답변 아래에 따로 붙으니 반복하지 않는다.\n"
    "결정이 있으면 결정 ID를 한 번 밝힌다. 승인 대기면 '아직 승인 대기', 논의만 있으면 '논의는 있었지만 결정되지 않음'이라고 쓴다. "
    "질문이 특정 프로젝트(예: 고객센터 챗봇)를 묻으면 그 프로젝트의 결정으로 답한다. "
    "'열람 권한 없음'인 결정은 ID만 말하고 내용을 추측하지 않는다. 근거에 없는 내용과 날짜는 쓰지 않는다.")


@traceable(name="write_node")
async def write_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 8 / 10a — 작성. 회의·의견은 구조화 결과를 템플릿으로(숫자·ID가 바뀌지 않게),
    질의 답변만 고품질 모델이 토큰 스트리밍으로 쓴다. 반려되면 피드백을 받아 다시 쓴다."""
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
    return out


@traceable(name="validate_node")
async def validate_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """Wave 8 — 코드 기준 검증 (모델에게 '맞냐'고 묻지 않는다). 최대 3회.
    회의: 인용이 원문과 글자까지 같은가 / 질의: 결정 ID, '승인 대기', '결정되지 않음'을 밝혔는가."""
    attempts = state.get("validation_attempts", 0) + 1
    problems = []
    if state["mode"] in ("meeting", "interview"):
        by_seq = {s["seq"]: s["text"] for s in state.get("segments", [])}
        for d in state.get("items", []) + state.get("competencies", []):
            no_evidence = not d["evidence"] and "decided_seq" in d          # 결정은 근거 필수 (역량 요약은 '근거 없음' 허용)
            if no_evidence or any(by_seq.get(e["seq"]) != e["quote"] for e in d["evidence"]):
                problems.append(f"인용 불일치: {d.get('statement') or d.get('question')}")
    elif state["mode"] == "question":
        item = state["items"][0]
        res = item["research"]
        ids = ([res["current"]["decision_id"]] if res["authority"] == "decision" else []) + \
              [p["decision_id"] for p in res.get("pending", [])]
        if item.get("project", DEFAULT_PID) != DEFAULT_PID:
            ids = [a["decision_id"] for a in res.get("advisory", []) if a["project_id"] == item["project"]] or ids
        if ids and not any(i in state["draft"] for i in ids):
            problems.append(f"근거 결정 ID({', '.join(ids)})를 답변에 밝히세요.")
        if res.get("pending") and "승인 대기" not in state["draft"]:
            problems.append("승인 대기 결정이라는 점을 밝히세요.")
        if not ids and res["authority"] in ("none", "discussion") and "결정되지 않" not in state["draft"]:
            problems.append("유효한 결정이 없으니 '결정되지 않음'을 밝히세요.")
    ok = not problems or attempts >= 3
    verdict = "합격" if not problems else ("합격(최대 시도 도달)" if ok else "불합격")
    out = {"validation_attempts": attempts, "validation_feedback": None if ok else "\n".join(problems),
           "events": _ev(state, {"type": "Validation", "attempt": attempts, "verdict": verdict, "problems": problems})}
    if problems and ok and state["mode"] == "question":
        # 3번 다시 써도 빠진 사실은 코드가 덧붙인다 (모델에게 맡기지 않는 마지막 안전장치)
        res = state["items"][0]["research"]
        notes = []
        if res["authority"] == "decision" and res["current"]["decision_id"] not in state["draft"]:
            notes.append(f"근거 결정: {res['current']['decision_id']}")
        notes += [f"{p['decision_id']}는 아직 승인 대기 (권위 없음)" for p in res.get("pending", []) if "승인 대기" not in state["draft"]]
        if not notes and res["authority"] in ("none", "discussion") and "결정되지 않" not in state["draft"]:
            notes.append("유효한 결정 없음 — 논의는 있었지만 결정되지 않음")
        if notes:
            out["draft"] = state["draft"] + "\n\n" + "\n".join(f"※ {n}" for n in notes)
    return out


def _after_validate(state: LedgerState) -> str:
    return "finalize" if state.get("validation_feedback") is None else "write"


@traceable(name="finalize_node")
async def finalize_node(state: LedgerState, deps: LedgerDeps) -> dict:
    """최종 답변. 옵션이 켜져 있으면 문제없는 결정을 '승인 대기'로 원장에 올린다 (승인은 사람만)."""
    proposed = []
    if deps.propose_to_ledger and state["mode"] == "meeting":
        for d in state["items"]:
            if d["review"].get("confirms"):          # 기존 승인 대기 결정을 확정하는 발언 → 새로 올리지 않고 그 결정을 승인 대상으로
                proposed += d["review"]["confirms"]
            elif d["review"]["status"] in ("ok", "change_approved"):
                r = await deps.mcp.call("propose_decision", statement=d["statement"], topic=d["topic"],
                                        decided_by=d["decided_by"],
                                        evidence=[{k: e[k] for k in ("meeting_id", "seq", "quote")} for e in d["evidence"]])
                proposed.append(r.get("proposed"))
    report = {"mode": state["mode"], "items": state.get("items", []), "overview": state.get("overview", {}),
              "action_items": state.get("action_items", []), "undecided": state.get("undecided", []),
              "proposed": proposed, "restricted": state.get("restricted", [])}
    return {"final": state["draft"], "report": report,
            "events": _ev(state, {"type": "Finalize", "pipeline": state["mode"], "proposed": proposed})}


def _after_plan(state: LedgerState) -> str:
    """원문이 없는 회의·면접(파기됐거나 잘못된 ID)은 조사 없이 바로 안내문 작성으로."""
    if state["mode"] in ("meeting", "interview") and not state.get("segments"):
        return "write"
    return "gate"


def _after_route(state: LedgerState) -> str:
    return {"chat": "chat_answer", "schedule": "schedule_answer"}.get(state["mode"], "plan")   # interview도 plan 경로


def build_ledger_graph(deps: LedgerDeps):
    g = StateGraph(LedgerState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    for name, fn in [("route", route_node), ("chat_answer", chat_answer_node), ("schedule_answer", schedule_answer_node), ("plan", plan_node),
                     ("gate", gate_node), ("research", research_node), ("replan_check", replan_check_node),
                     ("analysis", analysis_node), ("write", write_node), ("validate", validate_node),
                     ("finalize", finalize_node)]:
        g.add_node(name, node(fn))
    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route, {"chat_answer": "chat_answer", "schedule_answer": "schedule_answer", "plan": "plan"})
    g.add_edge("chat_answer", END)
    g.add_edge("schedule_answer", END)
    g.add_conditional_edges("plan", _after_plan, {"gate": "gate", "write": "write"})
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan, {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate, {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
