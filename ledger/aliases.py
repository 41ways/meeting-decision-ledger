"""용어(별칭) 색인 — 문장에 나온 낱말로 주제를 찾는다 (점수가 아니라 일치).

벡터 점수로 주제를 찾으면 "Redis 캐시 쓰기로 했어?"가 Redis를 기각한 대화 저장소(session.store)로 붙는다.
여기서는 주제마다 적어 둔 별칭(data/project/topic_aliases.json)이 문장에 그대로 나오는지만 본다.
그래서 결과마다 '어떤 별칭이 일치했는지'가 남고, 왜 그 주제인지 사람이 바로 확인할 수 있다.

정규화
- 대소문자·공백·문장부호는 무시한다 ('Long Polling' = '롱폴링'은 아니고, 'longpolling' = 'Long Polling')
- 별칭은 낱말 첫머리에서만 일치한다 ('불합격' 속 '합격'은 일치 아님). 한글은 뒤에 붙은 조사·어미를 허용한다 ('캐시는')
- 영문·숫자 별칭은 낱말 끝까지 맞아야 한다 ('SSE'는 'SSEs'와 일치 아님)
- 겹치는 일치는 긴 별칭이 이긴다 ('Redis 캐시' 안의 'Redis'는 session.store로 세지 않는다)

용어집(r2_docs/용어집.md)의 표준 표기가 들어간 별칭은 그 줄의 STT 오인식 표기로 바꾼 것도 별칭으로 넣는다
('권위 게이트' → '버니게이트'도 agent.reliability).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import vocab

DATA = Path(__file__).resolve().parent.parent / "data" / "project"
ALIAS_FILE = DATA / "topic_aliases.json"
GLOSSARY_FILE = DATA / "r2_docs" / "용어집.md"

# 영문·숫자 낱말과 한글 낱말을 따로 자른다 — 'Redis캐시'처럼 붙여 써도 'redis'와 '캐시'가 각각 낱말 첫머리가 된다
TOKEN = re.compile(r"[0-9a-z_]+|[가-힣]+")

# 낱말 끝 조사. 긴 것부터 본다. 한 글자 조사는 세 글자 이상 낱말에서만 뗀다 ('회의'의 '의', '평가'의 '가'를 지키려고)
_PARTICLES_LONG = ("으로는", "에서는", "이라고", "에게서", "으로", "에서", "에게", "까지", "부터", "처럼", "보다", "이랑",
                   "에는", "이나", "한테", "마다", "라고", "이라", "께서")
_PARTICLES_ONE = ("은", "는", "을", "를", "이", "가", "와", "과", "도", "만", "의", "에", "로", "랑")

# 단어 검색에서 세지 않는 낱말 — 질문 형식어와 어디에나 나오는 말
STOP = frozenset("""
했어 했어요 했나 했나요 했지 했죠 했는지 했었나 했었어 하기 하는 하나요 할까 하죠 해요 해줘 해주세요 했던 하던
뭐 뭐야 뭐였죠 뭐였지 무엇 왜 어떻게 언제 누가 어디 이유 지금 현재 정해졌나요 정해졌어 정했어 정했나 정해진
결정 결정은 기준 그거 이거 저거 그건 이건 우리 회의 혹시 그럼 그리고 근데 그래서 그냥 일단 다시 좀 더 또 네 아니
있어요 없어요 있나요 있어 없어 같아요 같은데요 거 것 수 얘기 부분 경우 때 쪽 건 걸 게 거예요 거죠 됐어 됐나 됐나요
쓰기로 하기로 가기로 바꾸기로 하는걸로 걸로 되나요 될까 있었나 있었어 였나 였어 인가 인가요 이에요 예요 에요
""".split())
_ENDINGS = ("습니다", "니다", "어요", "아요", "에요", "예요", "나요", "가요", "까요", "죠", "지요", "네요", "군요",
            "했어", "했나", "했지", "합시다", "할게요", "는데", "는지", "었어", "였어", "한다", "했다", "된다", "이다")


def _norm(text: str) -> str:
    """
    비교용 정규화 (호환 문자 통일 + 소문자)
    Args:
        text: 원문
    Returns:
        정규화한 문자열
    """
    return unicodedata.normalize("NFKC", text or "").lower()


def tokens(text: str) -> list[str]:
    """
    문장을 낱말로 자른다 (소문자, 영문·숫자와 한글을 따로)
    Args:
        text: 원문
    Returns:
        낱말 목록 (조사는 떼지 않은 그대로)
    """
    return TOKEN.findall(_norm(text))


def strip_particle(tok: str) -> str:
    """
    한글 낱말 끝의 조사를 하나 뗀다
    Args:
        tok: 낱말 (tokens 결과)
    Returns:
        조사를 뗀 낱말. 영문·숫자 낱말은 그대로
    Examples:
        strip_particle('캐시는') -> '캐시', strip_particle('회의') -> '회의', strip_particle('레디스로') -> '레디스'
    """
    if not tok or not "가" <= tok[0] <= "힣":
        return tok
    for p in _PARTICLES_LONG:
        if tok.endswith(p) and len(tok) - len(p) >= 1:
            return tok[:-len(p)]
    if len(tok) >= 3 and tok[-1] in _PARTICLES_ONE:
        return tok[:-1]
    return tok


def canon(word: str) -> str:
    """
    같은 말의 다른 표기를 첫 표기로 맞춘다 (별칭 파일 synonyms)
    Args:
        word: 조사 뗀 낱말
    Returns:
        대표 표기 낱말 (등록 안 된 낱말은 그대로)
    Examples:
        canon('레디스') -> 'redis', canon('캐싱') -> 'cache'
    """
    return _load()[2].get(word, word)


def content_words(text: str) -> list[str]:
    """
    단어 검색에 쓸 내용어 (조사 뗀 낱말에서 질문 형식어·어미로 끝나는 말을 뺀 것)
    Args:
        text: 원문
    Returns:
        중복 없는 내용어 목록 (나온 순서)
    """
    out = []
    for t in tokens(text):
        w = strip_particle(t)
        if len(w) < 2 or w in STOP or t in STOP:
            continue
        if "가" <= w[0] <= "힣" and w.endswith(_ENDINGS):
            continue
        out.append(canon(w))
    return list(dict.fromkeys(out))


def parse_glossary(md: str) -> list[tuple[str, list[str]]]:
    """
    용어집 표에서 (표준 표기, 오인식 목록) 추출
    Args:
        md: 용어집 마크다운 (| 표준 용어 | 뜻 | STT 오인식 예 |)
    Returns:
        [(표준 표기, [오인식 …])] — 표준 표기는 괄호 앞 부분
    """
    out = []
    for row in md.splitlines():
        cols = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cols) < 3 or cols[0] in ("표준 용어", "") or set(cols[0]) <= {"-"}:
            continue
        std = cols[0].split(" (")[0].strip()
        out.append((std, [v.strip() for v in cols[2].split(",") if len(v.strip()) >= 2]))
    return out


@dataclass(frozen=True)
class Alias:
    """
    별칭 하나

    Attributes:
        topic: 주제 키
        text:  화면에 보일 별칭 표기
        key:   비교용 정규화 표기 (공백 없이 이은 소문자 낱말)
        note:  용어집에서 늘린 별칭이면 원래 표기 ('버니게이트 → 권위 게이트'), 아니면 빈 문자열
    """
    topic: str
    text: str
    key: str
    note: str = ""


def _key(text: str) -> str:
    """
    별칭 비교 키 (낱말을 공백 없이 이음)
    Args:
        text: 별칭 표기
    Returns:
        비교 키
    """
    return "".join(tokens(text))


def build_aliases(table: dict[str, list[str]], glossary: list[tuple[str, list[str]]]) -> list[Alias]:
    """
    주제별 별칭 표와 용어집으로 별칭 목록을 만든다
    Args:
        table:    {주제 키: [별칭 …]}
        glossary: parse_glossary 결과
    Returns:
        별칭 목록 (같은 주제·같은 키는 한 번만)
    """
    out, seen = [], set()

    def add(topic: str, text: str, note: str = "") -> None:
        """비교 키가 비지 않고 처음 보는 (주제, 키)만 추가"""
        k = _key(text)
        if k and (topic, k) not in seen:
            seen.add((topic, k))
            out.append(Alias(topic, text, k, note))

    for topic, words in table.items():
        add(topic, topic, "주제 키")   # 주제 키를 그대로 쓴 질문('session.store 정했어?')도 잡는다
        for w in words:
            add(topic, w)
            for std, variants in glossary:
                # 표준 표기가 별칭 안에 있으면 오인식으로 바꾼 표기도 같은 주제로 (대소문자 무시)
                if std and _norm(std) in _norm(w):
                    for v in variants:
                        add(topic, re.sub(re.escape(std), v, w, flags=re.I), f"{v} → {std}")
    return out


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[Alias, ...], str, dict[str, str]]:
    """
    별칭 파일·용어집을 읽어 별칭 목록·판(버전) 지문·같은 말 표를 만든다 (프로세스당 한 번)
    Returns:
        (별칭 목록, 지문, {다른 표기: 대표 표기}) — 지문은 두 파일 내용의 해시. 바뀌면 색인을 다시 만든다
    """
    raw = ALIAS_FILE.read_text(encoding="utf-8") if ALIAS_FILE.exists() else '{"aliases": {}}'
    gl = GLOSSARY_FILE.read_text(encoding="utf-8") if GLOSSARY_FILE.exists() else ""
    data = json.loads(raw)
    table = {k: list(v) for k, v in data["aliases"].items()}
    for k, v in vocab.extra()["aliases"].items():          # 운영 중 팀장이 더한 주제·별칭
        table[k] = table.get(k, []) + [a for a in v if a not in table.get(k, [])]
    al = build_aliases(table, parse_glossary(gl))
    same = {_key(f): _key(g[0]) for g in data.get("synonyms", []) for f in g if _key(f)}
    return tuple(al), hashlib.sha1((raw + gl + vocab.extra_text()).encode()).hexdigest()[:12], same


def base_table() -> dict[str, list[str]]:
    """
    기본 별칭 표 (data/project/topic_aliases.json)
    Returns:
        {주제 키: [별칭]}
    """
    return json.loads(ALIAS_FILE.read_text(encoding="utf-8"))["aliases"] if ALIAS_FILE.exists() else {}


def reload() -> None:
    """별칭을 더한 뒤 다시 읽게 한다 (판 지문이 바뀌어 색인도 다시 만들어진다)"""
    _load.cache_clear()


def aliases() -> tuple[Alias, ...]:
    """
    현재 별칭 목록
    Returns:
        별칭 목록
    """
    return _load()[0]


def version() -> str:
    """
    별칭 판 지문 (별칭 파일·용어집이 바뀌면 달라진다)
    Returns:
        12자리 해시
    """
    return _load()[1]


@dataclass(frozen=True)
class Hit:
    """
    문장에서 일치한 별칭 하나

    Attributes:
        topic: 주제 키
        alias: 일치한 별칭 표기
        start: 정규화 문자열에서의 시작 위치
        end:   끝 위치 (포함 안 함)
        note:  용어집 오인식으로 늘린 별칭이면 그 설명
    """
    topic: str
    alias: str
    start: int
    end: int
    note: str = ""


def match(text: str, table: tuple[Alias, ...] | None = None) -> list[Hit]:
    """
    문장에서 일치하는 별칭을 모두 찾는다 (긴 별칭이 짧은 것을 덮으면 짧은 것은 뺀다)
    Args:
        text:  문장
        table: 별칭 목록. None이면 파일에서 읽은 기본 목록
    Returns:
        일치 목록 (나온 순서)
    """
    toks = tokens(text)
    s, starts, ends, pos = "".join(toks), set(), set(), 0
    for t in toks:
        starts.add(pos)
        pos += len(t)
        ends.add(pos)
    found = []
    for a in (aliases() if table is None else table):
        i = s.find(a.key)
        while i != -1:
            j = i + len(a.key)
            # 낱말 첫머리에서 시작해야 하고, 영문·숫자로 끝나는 별칭은 낱말 끝까지 맞아야 한다
            if i in starts and (not a.key[-1].isascii() or j in ends):
                found.append(Hit(a.topic, a.text, i, j, a.note))
            i = s.find(a.key, i + 1)
    # 긴 일치부터 받아들이고, 이미 받아들인 일치 안에 완전히 들어가는 짧은 일치는 버린다
    kept: list[Hit] = []
    for h in sorted(found, key=lambda h: (-(h.end - h.start), h.start)):
        if not any(k.start <= h.start and h.end <= k.end and (k.end - k.start) > (h.end - h.start) for k in kept):
            kept.append(h)
    return sorted(kept, key=lambda h: (h.start, -(h.end - h.start)))


def topics_of(text: str, table: tuple[Alias, ...] | None = None) -> list[dict]:
    """
    문장이 가리키는 주제와 그 근거 별칭
    Args:
        text:  문장
        table: 별칭 목록. None이면 기본 목록
    Returns:
        [{"topic", "aliases": [일치한 별칭 표기 …]}] — 일치한 글자 수가 많은 주제부터, 같으면 먼저 나온 주제부터
    Examples:
        topics_of('Redis 캐시 쓰기로 했어?') -> [{'topic': 'cache', 'aliases': ['Redis 캐시']}]
    """
    by: dict[str, dict] = {}
    for h in match(text, table):
        b = by.setdefault(h.topic, {"topic": h.topic, "aliases": [], "_len": 0, "_first": h.start})
        label = h.alias + (f" (용어집: {h.note})" if h.note and h.note != "주제 키" else "")
        if label not in b["aliases"]:
            b["aliases"].append(label)
        b["_len"] += h.end - h.start
    ranked = sorted(by.values(), key=lambda b: (-b["_len"], b["_first"]))
    return [{"topic": b["topic"], "aliases": b["aliases"]} for b in ranked]


def choose_topic(text: str, llm_topic: str = "") -> dict:
    """
    별칭 일치를 먼저 보고 주제를 정한다 (LLM 선택은 별칭이 여럿이거나 없을 때만)

    - 별칭이 한 주제만 가리키면 그 주제 (LLM이 다르게 골랐으면 disagree에 남긴다)
    - 여러 주제를 가리키면 LLM이 고른 주제가 그 안에 있을 때 그것, 아니면 일치가 가장 긴 주제
    - 별칭이 없으면 LLM 주제
    투표(자기 일관성)는 하지 않는다. 다르다는 사실만 기록한다.
    Args:
        text:      문장
        llm_topic: LLM이 고른 주제 키 (빈 문자열 가능)
    Returns:
        {"topic", "by": "별칭" | "별칭+LLM" | "LLM" | "", "aliases": 일치한 별칭, "candidates": 별칭이 가리킨 주제들,
         "llm": LLM 주제, "disagree": 별칭과 LLM이 다르면 True}
    """
    hits = topics_of(text)
    cands = [h["topic"] for h in hits]
    if not hits:
        return {"topic": llm_topic, "by": "LLM" if llm_topic else "", "aliases": [], "candidates": [],
                "llm": llm_topic, "disagree": False}
    if llm_topic in cands and len(cands) > 1:
        topic, by = llm_topic, "별칭+LLM"
    else:
        topic, by = cands[0], "별칭"
    al = next(h["aliases"] for h in hits if h["topic"] == topic)
    return {"topic": topic, "by": by, "aliases": al, "candidates": cands, "llm": llm_topic,
            "disagree": bool(llm_topic) and llm_topic != topic}
