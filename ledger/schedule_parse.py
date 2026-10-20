"""자연어 일정 추가 문장 해석 — 날짜·시간은 코드가, 제목·참석자·프로젝트는 LLM 추출을 코드가 검증한다.

해석 순서
1. 날짜·시작·끝 시간: 규칙(parse_when). 상대 주 계산은 그래프의 _range와 같은 규칙(week_offset)
2. 제목·참석자·프로젝트: LLM(JSON 스키마)이 뽑고, 코드가 원문·users.json·projects.json과 대조
3. 해석 못 한 값은 빈칸으로 두고 되묻는다 (기본값으로 지어내지 않음)

저장은 여기서 하지 않는다. 결과(초안)를 화면에 보여 주고 사람이 '추가'를 눌러야 저장된다.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "project"
USERS = json.loads((DATA / "users.json").read_text())["users"]
PROJECTS = json.loads((DATA / "projects.json").read_text())["projects"]
ROLE_RANK = {"인턴": 0, "팀원": 1, "담당자": 2, "팀장": 3}
WEEKDAYS = "월화수목금토일"

# 추가 동사. '검증 기준에 인용 여부를 추가하면 어떨까요?'(의견)는 날짜가 없어 추가로 보지 않는다
ADD_VERB = re.compile(r"추가|잡아|잡자|잡고|등록|넣어|만들어")
EVENT_WORD = re.compile(r"일정|회의|미팅|리뷰|면접|약속|점검|회고|워크숍")
# 묻는 문장은 조회다 ('다음 주 회의 추가됐어?', '내일 회의 몇 개야?')
QUESTION = re.compile(r"몇\s*(개|건|번)|있어\?|있나|됐어|됐나|되었|언제|\?\s*$")
CLOCK = re.compile(r"(오전|오후|아침|낮|저녁|밤)?\s*(\d{1,2})\s*시(?!간)\s*(반|(\d{1,2})\s*분)?|(\d{1,2}):(\d{2})")
DURATION = re.compile(r"(\d+)\s*시간\s*(반|(\d{1,2})\s*분)?|(\d+)\s*분")
RANGE_SEP = re.compile(r"\s*(~|-|–|부터|에서)\s*")
WEEK_WORD = re.compile(r"\d+\s*주\s*(전|후|뒤)|(지지난|저저번|지난|저번|다다음|다음|이번)\s*주|전전주|전주")

SCHEDULE_ADD_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"},
    "attendees": {"type": "array", "items": {"type": "string"}},
    "project": {"type": "string", "enum": [p["key"] for p in PROJECTS] + [""]}},
    "required": ["title", "attendees", "project"]}


def is_add_request(msg: str) -> bool:
    """
    일정 추가 요청인지 규칙으로 판별

    날짜 표현이 있어야 하고, 그 위에 추가 동사 + (시각 또는 일정 단어)가 있거나
    동사 없이 시각까지 적은 평서문('내일 10시 반 장상민이랑 캐시 설계 리뷰 30분')이면 추가로 본다.
    묻는 문장('몇 개야', '됐어?')은 조회라서 제외한다.
    Args:
        msg: 사용자 메시지
    Returns:
        일정 추가 요청이면 True
    """
    if QUESTION.search(msg) or not _parse_date(msg, date(2026, 1, 5))[0]:
        return False
    has_clock = bool(CLOCK.search(msg))
    if ADD_VERB.search(msg):
        return has_clock or bool(EVENT_WORD.search(msg))
    return has_clock


def week_offset(msg: str) -> int | None:
    """
    상대 주 표현을 이번 주 기준 주 차이로 변환

    agent.ledger_graph._range와 같은 규칙이다 (테스트가 두 결과가 같은지 확인한다).
    '지지난'·'다다음'을 '지난'·'다음'보다 먼저 본다 ('다다음 주'에도 '다음 주'가 들어 있음).
    Args:
        msg: 사용자 메시지
    Returns:
        주 차이 (지난주 -1, 이번 주 0, 다음 주 1, 다다음 주 2). 주 표현이 없으면 None
    """
    if m := re.search(r"(\d+)\s*주\s*(전|후|뒤)", msg):
        return -int(m.group(1)) if m.group(2) == "전" else int(m.group(1))
    for pat, n in ((r"지지난\s*주|저저번\s*주|전전주", -2), (r"지난\s*주|저번\s*주|전주", -1),
                   (r"다다음\s*주", 2), (r"다음\s*주", 1), (r"이번\s*주", 0)):
        if re.search(pat, msg):
            return n
    return None


def _safe_date(y: int, m: int, d: int) -> str:
    """
    연·월·일로 ISO 날짜 생성. 없는 날짜(2월 30일 등)는 빈 문자열
    Args:
        y: 연
        m: 월
        d: 일
    Returns:
        ISO 날짜 문자열 또는 빈 문자열
    """
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def _parse_date(msg: str, today: date) -> tuple[str, str, list[tuple[int, int]]]:
    """
    문장 속 날짜 표현 하나를 날짜로 변환

    지원: YYYY-MM-DD, M월 D일, M/D, 오늘·내일·모레·글피, (이번·다음·다다음·N주 뒤) X요일, D일.
    주 표현 없이 요일만 있으면 오늘부터 가장 가까운 그 요일.
    Args:
        msg: 사용자 메시지
        today: 기준 날짜
    Returns:
        (ISO 날짜 또는 빈 문자열, 화면 표시용 원문 표현, 제목에서 지울 글자 구간 목록)
    """
    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", msg):
        return _safe_date(*map(int, m.groups())), m.group(0), [m.span()]
    if m := re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", msg):
        return _safe_date(today.year, int(m.group(1)), int(m.group(2))), m.group(0), [m.span()]
    if m := re.search(r"(?<![\d:/])(\d{1,2})/(\d{1,2})(?![\d/])", msg):
        return _safe_date(today.year, int(m.group(1)), int(m.group(2))), m.group(0), [m.span()]
    for word, days in (("내일모레", 2), ("글피", 3), ("모레", 2), ("내일", 1), ("오늘", 0)):
        if word in msg:
            i = msg.index(word)
            return (today + timedelta(days=days)).isoformat(), word, [(i, i + len(word))]
    if m := re.search(r"([월화수목금토일])요일", msg):
        wd = WEEKDAYS.index(m.group(1))
        weeks = week_offset(msg)
        wk = WEEK_WORD.search(msg)
        if weeks is None or wk is None:
            return (today + timedelta(days=(wd - today.weekday()) % 7)).isoformat(), m.group(0), [m.span()]
        mon = today - timedelta(days=today.weekday())
        d = mon + timedelta(days=7 * weeks + wd)
        return d.isoformat(), msg[wk.start():m.end()], [wk.span(), m.span()]
    if m := re.search(r"(?<!\d)(\d{1,2})일(?!\s*(간|동안))", msg):
        return _safe_date(today.year, today.month, int(m.group(1))), m.group(0), [m.span()]
    return "", "", []


def _hm(minutes: int) -> str:
    """
    자정부터 분 수를 'HH:MM'으로 변환
    Args:
        minutes: 0 이상 분 수
    Returns:
        'HH:MM' 문자열 (하루를 넘으면 23:59로 자름)
    """
    minutes = min(minutes, 23 * 60 + 59)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clock(m: re.Match, notes: list[str] | None) -> tuple[int, bool]:
    """
    시각 매치 하나를 분 수로 변환

    오전·오후 표시가 없는 1~7시는 업무 시간으로 보고 오후로 해석하고, 그렇게 봤다고 notes에 남긴다.
    Args:
        m: CLOCK 매치
        notes: 해석 메모를 쌓을 목록. None이면 오후 추정을 하지 않는다 (구간의 끝 시각)
    Returns:
        (자정부터 분 수, 오전·오후나 24시간 표기로 시각이 분명한지)
    """
    if m.group(5):
        return int(m.group(5)) * 60 + int(m.group(6)), True
    mer, h = m.group(1), int(m.group(2))
    mins = 30 if m.group(3) == "반" else int(m.group(4) or 0)
    if mer in ("오후", "저녁", "밤", "낮") and h < 12:
        h += 12
    elif mer in ("오전", "아침") and h == 12:
        h = 0
    elif not mer and 1 <= h <= 7 and notes is not None:
        h += 12
        notes.append(f"오전·오후 표시가 없어 '{m.group(0).strip()}'를 오후 {h - 12}시로 봤습니다.")
    return h * 60 + mins, bool(mer) or h >= 13


def parse_when(msg: str, today: str) -> dict:
    """
    일정 문장에서 날짜·시작·끝 시간을 규칙으로 해석

    끝 시간은 '14:00~15:00', '2시부터 4시까지' 같은 구간이나 'N분·N시간' 길이로 정한다.
    값을 알 수 없으면 빈 문자열로 두고 missing에 넣는다.
    Args:
        msg: 사용자 메시지
        today: 기준 날짜 (YYYY-MM-DD)
    Returns:
        date, start, end ('HH:MM' 또는 빈 문자열), date_text(원문 표현), duration(분, 없으면 0),
        notes(해석 메모), missing(빈 항목 이름), spans(제목에서 지울 글자 구간)
    Examples:
        today가 2026-10-20(화)이고 '다음 주 화요일 오후 3시'면 date='2026-10-27', start='15:00', end=''
    """
    notes: list[str] = []
    day, date_text, spans = _parse_date(msg, date.fromisoformat(today))
    clocks = list(CLOCK.finditer(msg))
    start = end = ""
    if clocks:
        s_min, _ = _clock(clocks[0], notes)
        start = _hm(s_min)
        spans.append(clocks[0].span())
        # 두 번째 시각이 '~'·'부터'로 바로 이어지면 끝 시간
        if len(clocks) > 1 and RANGE_SEP.fullmatch(msg[clocks[0].end():clocks[1].start()]):
            e_min, sure = _clock(clocks[1], None)
            if not sure and e_min <= s_min < e_min + 12 * 60:
                e_min += 12 * 60                    # '오후 2시~4시' → 16:00
            end = _hm(e_min)
            tail = re.match(r"\s*까지", msg[clocks[1].end():])
            spans.append((clocks[0].end(), clocks[1].end() + (tail.end() if tail else 0)))
    # 길이는 시각 부분을 가린 뒤에 찾는다 ('10시 30분'의 30분을 길이로 읽지 않도록)
    masked = list(msg)
    for a, b in spans:
        masked[a:b] = " " * (b - a)
    duration = 0
    if m := DURATION.search("".join(masked)):
        duration = int(m.group(1)) * 60 + (30 if m.group(2) == "반" else int(m.group(3) or 0)) if m.group(1) \
            else int(m.group(4))
        spans.append(m.span())
        if start and not end:
            h, mi = map(int, start.split(":"))
            end = _hm(h * 60 + mi + duration)
    if day and day < today:
        notes.append(f"{day}는 지난 날짜입니다.")
    missing = [k for k, v in (("date", day), ("start", start), ("end", end)) if not v]
    return {"date": day, "start": start, "end": end, "date_text": date_text, "duration": duration,
            "notes": notes, "missing": missing, "spans": spans}


def code_title(msg: str, spans: list[tuple[int, int]]) -> str:
    """
    날짜·시간·참석자·동사를 지운 나머지로 제목 만들기 (LLM 제목이 원문에 없을 때 대체)
    Args:
        msg: 사용자 메시지
        spans: parse_when이 찾은 날짜·시간 글자 구간
    Returns:
        제목 문자열. 남는 글자가 없으면 빈 문자열
    """
    chars = list(msg)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    s = "".join(chars)
    for u in USERS:
        s = re.sub(rf"({u['name']}|{u['name'][1:]})\s*(님|씨)?(이랑|랑|하고|과|와|도)?(?=\s|$)", " ", s)
    s = re.sub(r"(일정\s*)?(을|를)?\s*(추가|등록|잡아|잡자|잡고|넣어|만들어)\S*", " ", s)
    s = re.sub(r"해\s*줘|해\s*주세요|부탁해요?|좀", " ", s)
    s = re.sub(r"(?<!\S)(에|에서|부터|까지|으로|로|간|동안)(?!\S)", " ", s)
    s = re.sub(r"\s+일정\s*$", "", " " + s)
    return re.sub(r"\s+", " ", s).strip(" .,!~")


def match_user(name: str) -> dict | None:
    """
    이름을 users.json 사용자와 대조

    성까지 쓴 이름이 우선이고, 이름 두 글자('상민')는 한 사람에게만 맞을 때 인정한다.
    Args:
        name: 문장이나 LLM이 낸 사람 이름 (님·씨는 떼고 비교)
    Returns:
        사용자 정보. 없거나 여러 명이 맞으면 None
    """
    name = re.sub(r"\s*(님|씨)$", "", name.strip())
    exact = [u for u in USERS if u["name"] == name]
    if exact:
        return exact[0]
    given = [u for u in USERS if len(name) >= 2 and u["name"][1:] == name]
    return given[0] if len(given) == 1 else None


def names_in_text(msg: str) -> list[dict]:
    """
    문장에 나온 등록 사용자 찾기 — 성명, 또는 '상민 님'·'상민이랑'처럼 이름에 호칭·조사가 붙은 경우
    Args:
        msg: 사용자 메시지
    Returns:
        사용자 정보 목록 (문장에 나온 순서)
    """
    found = []
    for u in USERS:
        m = re.search(u["name"], msg) or re.search(rf"{u['name'][1:]}\s*(님|씨|이랑|랑|하고)", msg)
        if m and match_user(u["name"][1:]) is u or m and u["name"] in msg:
            found.append((m.start(), u))
    return [u for _, u in sorted(found, key=lambda x: x[0])]


def kind_of(title: str) -> str:
    """
    일정 제목으로 종류 구분 (일정 조회 답변의 회의·면접·기타 일정 집계용)
    Args:
        title: 일정 제목
    Returns:
        'interview' | 'meeting' | 'event'
    """
    if "면접" in title:
        return "interview"
    if re.search(r"회의|미팅|리뷰|회고|스탠드업|싱크|워크숍", title):
        return "meeting"
    return "event"


def _project_of(msg: str, llm_key: str, scope_project: str) -> tuple[str, str]:
    """
    프로젝트 키 정하기 — 문장 속 약자·이름 > LLM 선택(원문 근거 있을 때) > 화면에서 고른 범위

    projects.json에 없는 키는 버린다. 종료된 프로젝트면 경고 문구를 함께 돌려준다.
    Args:
        msg: 사용자 메시지
        llm_key: LLM이 고른 프로젝트 키
        scope_project: 화면에서 고른 프로젝트 키
    Returns:
        (프로젝트 키 또는 빈 문자열, 경고 문구 또는 빈 문자열)
    """
    keys = {p["key"]: p for p in PROJECTS}
    if m := re.search(r"(?<![A-Z])(" + "|".join(keys) + r")(?![A-Z])", msg.upper()):
        key = m.group(1)
    else:
        key = next((p["key"] for p in PROJECTS if p["name"] in msg), "")
    # LLM이 고른 키는 문장에 근거가 있을 때만 (위에서 이미 찾았을 경우와 같음) — 근거 없는 추정은 버린다
    if not key and llm_key in keys and keys[llm_key]["name"].split()[0] in msg:
        key = llm_key
    key = key or (scope_project if scope_project in keys else "")
    if key and keys[key]["status"] != "진행":
        return key, f"{key}는 {keys[key]['status']}된 프로젝트입니다."
    return key, ""


def build_draft(msg: str, today: str, user_id: str, extracted: dict | None = None, scope_project: str = "") -> dict:
    """
    일정 추가 초안 만들기 — 코드 해석 + LLM 추출값 검증

    LLM 제목은 낱말이 원문에 모두 있어야 쓴다 (없으면 코드 제목). LLM 참석자도 원문에 나온 이름만 받는다.
    등록되지 않은 이름은 참석자에 넣지 않고 경고로 돌린다. 요청자는 참석자에 자동으로 넣는다.
    Args:
        msg: 사용자 메시지
        today: 기준 날짜 (YYYY-MM-DD)
        user_id: 요청자 ID
        extracted: LLM 추출 결과 (title, attendees, project). 없으면 코드만으로
        scope_project: 화면에서 고른 프로젝트 키 (문장에 프로젝트가 없을 때 사용)
    Returns:
        초안 dict — date, start, end, date_text, title, attendees, project, kind, created_by,
        warnings, notes, missing(되물을 항목), title_source('llm'|'code'), saved=False
    """
    when = parse_when(msg, today)
    ex = extracted or {}
    flat = re.sub(r"\s", "", msg)
    llm_title = (ex.get("title") or "").strip()
    grounded = bool(llm_title) and all(w in flat for w in re.sub(r"[^\w\s]", " ", llm_title).split())
    title = llm_title if grounded else code_title(msg, when["spans"])
    warnings: list[str] = []
    people = names_in_text(msg)
    for raw in ex.get("attendees") or []:
        bare = re.sub(r"\s*(님|씨)$", "", (raw or "").strip())
        if not bare or bare not in msg:
            continue                     # 원문에 없는 이름은 LLM이 지어낸 것으로 보고 버린다
        u = match_user(bare)
        if u is None:
            warnings.append(f"등록되지 않은 이름이라 참석자에서 뺐습니다: {bare}")
        elif u not in people:
            people.append(u)
    me = next((u for u in USERS if u["user_id"] == user_id), None)
    if me and me not in people:
        people.insert(0, me)
    project, prj_note = _project_of(msg, ex.get("project") or "", scope_project)
    if prj_note:
        warnings.append(prj_note)
    notes = list(when["notes"])
    if when["start"] and not when["end"]:
        notes.append("끝나는 시간이 없습니다. 비워 두고 추가하면 겹침은 시작부터 1시간으로 봅니다.")
    missing = [k for k in ("date", "start") if not when[k]] + ([] if title else ["title"])
    return {"date": when["date"], "start": when["start"], "end": when["end"], "date_text": when["date_text"],
            "title": title, "attendees": [u["name"] for u in people], "project": project, "kind": kind_of(title),
            "created_by": user_id, "warnings": warnings, "notes": notes, "missing": missing,
            "title_source": "llm" if grounded else "code", "saved": False}


def validate_draft(draft: dict) -> list[str]:
    """
    저장 직전 초안 검사 — 화면에서 고친 값도 다시 본다
    Args:
        draft: 일정 초안 (date, start, end, title, attendees, project)
    Returns:
        문제 목록. 비어 있으면 저장 가능
    """
    errs = []
    d = draft.get("date") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or not _safe_date(*map(int, d.split("-"))):
        errs.append("날짜가 비어 있거나 형식이 틀립니다.")
    for k, label in (("start", "시작 시간"), ("end", "끝 시간")):
        v = draft.get(k) or ""
        if (v or k == "start") and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            errs.append(f"{label}이 비어 있거나 형식이 틀립니다 (HH:MM).")
    if draft.get("start") and draft.get("end") and draft["end"] <= draft["start"]:
        errs.append("끝 시간이 시작 시간보다 빠릅니다.")
    if not (draft.get("title") or "").strip():
        errs.append("제목이 비어 있습니다.")
    unknown = [n for n in draft.get("attendees") or [] if match_user(n) is None]
    if unknown:
        errs.append("등록되지 않은 참석자: " + ", ".join(unknown))
    if draft.get("project") and draft["project"] not in {p["key"] for p in PROJECTS}:
        errs.append(f"없는 프로젝트 키: {draft['project']}")
    return errs


def add_permission(user_id: str, attendees: list[str]) -> tuple[str, str]:
    """
    일정 추가 권한 판정

    팀원 이상은 바로 추가한다. 인턴은 본인만 참석하는 일정만 추가하고,
    다른 사람이 들어간 일정은 저장하지 않고 팀장에게 요청 알림으로 보낸다.
    Args:
        user_id: 요청자 ID
        attendees: 참석자 이름 목록
    Returns:
        ('add' | 'request' | 'deny', 사유 문구)
    """
    me = next((u for u in USERS if u["user_id"] == user_id), None)
    if me is None:
        return "deny", "등록되지 않은 사용자입니다."
    if ROLE_RANK[me["role"]] >= ROLE_RANK["팀원"]:
        return "add", ""
    if set(attendees) <= {me["name"]}:
        return "add", "본인 일정"
    return "request", "인턴은 다른 사람이 들어간 일정을 직접 추가할 수 없어 팀장에게 요청으로 보냅니다."
