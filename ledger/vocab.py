"""운영 중에 더한 주제·별칭 — 기본 목록(data/project) 위에 덧붙이는 판.

주제 목록과 별칭 표는 저장소에 들어 있는 기본값이다. 서비스를 쓰다 보면 목록에 없는 사안이 나온다.
그때 팀장이 승인 화면에서 새 주제(키·설명·별칭)를 만들거나, 찾지 못한 표현을 기존 주제의 별칭으로 보탠다.
더한 것은 store/vocab_extra.json에 따로 쌓는다 — 기본 파일을 고치지 않으니 시연 초기화(reset_demo)로 되돌릴 수 있고,
무엇을 언제 더했는지가 이력(events)과 파이프라인 버전 지문에 남는다.

새 주제는 사람이 만든다. 모델이 주제를 지어내면 같은 사안이 매번 다른 이름으로 갈라져
'같은 주제의 결정은 대체된다'는 원장 규칙이 깨지기 때문이다. 모델은 '목록에 없음(새 주제)'까지만 말한다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "project"
KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,2}$")
MAX_ALIASES = 12


def extra_path() -> Path:
    """
    덧붙인 판 파일 경로 (평가 사본 LEDGER_STORE를 따른다)
    Returns:
        store/vocab_extra.json 경로
    """
    return Path(os.environ.get("LEDGER_STORE", ROOT / "store")) / "vocab_extra.json"


def extra() -> dict:
    """
    덧붙인 주제·별칭
    Returns:
        {"topics": {키: 설명}, "aliases": {키: [별칭]}}
    """
    p = extra_path()
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {"topics": data.get("topics", {}), "aliases": data.get("aliases", {})}


def extra_text() -> str:
    """
    덧붙인 판 원문 (지문 계산용, 없으면 빈 문자열)
    Returns:
        파일 내용
    """
    p = extra_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def topics() -> dict[str, str]:
    """
    기본 주제 + 덧붙인 주제
    Returns:
        {키: 설명}
    """
    base = json.loads((DATA / "topics.json").read_text(encoding="utf-8"))["topics"]
    return {**base, **extra()["topics"]}


def _clean(aliases: list[str]) -> list[str]:
    """
    별칭 정리 — 앞뒤 공백 제거, 한 글자·중복 제거
    Args:
        aliases: 입력 별칭
    Returns:
        정리된 별칭
    """
    out = []
    for a in aliases:
        a = " ".join(str(a).split())
        if len(a) >= 2 and a not in out:
            out.append(a)
    return out


def _save(data: dict) -> None:
    """
    덧붙인 판 저장
    Args:
        data: {"topics", "aliases"}
    """
    p = extra_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def add_topic(key: str, desc: str, aliases: list[str]) -> dict:
    """
    새 주제 만들기
    Args:
        key:     주제 키 (영문 소문자·숫자·밑줄, 점으로 최대 3단계 — 예: notice.channel)
        desc:    설명 (LLM이 주제를 고를 때 읽는다)
        aliases: 질문·발언에 실제로 나오는 표현 (1개 이상)
    Returns:
        {"topic": 키, "desc": 설명, "aliases": [별칭]}
    Raises:
        ValueError: 키 형식이 틀렸거나 이미 있거나, 설명·별칭이 비었을 때
    """
    key, desc, al = key.strip(), " ".join(desc.split()), _clean(aliases)
    if not KEY.match(key):
        raise ValueError(f"주제 키 형식이 아닙니다: '{key}' (영문 소문자·숫자·밑줄, 예: notice.channel)")
    if key in topics():
        raise ValueError(f"이미 있는 주제입니다: {key}")
    if not desc:
        raise ValueError("설명을 적어야 합니다 (모델이 주제를 고를 때 읽습니다)")
    if not al:
        raise ValueError("별칭을 하나 이상 적어야 합니다 (없으면 질문에서 이 주제를 찾지 못합니다)")
    data = extra()
    data["topics"][key] = desc
    data["aliases"][key] = al[:MAX_ALIASES]
    _save(data)
    return {"topic": key, "desc": desc, "aliases": data["aliases"][key]}


def add_aliases(key: str, aliases: list[str], base_aliases: dict[str, list[str]]) -> dict:
    """
    기존 주제에 별칭 보태기 (찾지 못한 표현을 이어 주려고)
    Args:
        key:          주제 키
        aliases:      보탤 표현
        base_aliases: 기본 별칭 표 (이미 있는 표현은 다시 넣지 않는다)
    Returns:
        {"topic": 키, "added": [새로 보탠 별칭]}
    Raises:
        ValueError: 없는 주제이거나 보탤 표현이 없을 때
    """
    if key not in topics():
        raise ValueError(f"없는 주제입니다: {key}")
    data = extra()
    have = {a.lower() for a in base_aliases.get(key, []) + data["aliases"].get(key, [])}
    new = [a for a in _clean(aliases) if a.lower() not in have]
    if not new:
        raise ValueError("새로 보탤 별칭이 없습니다 (이미 있거나 비었음)")
    data["aliases"][key] = (data["aliases"].get(key, []) + new)[:MAX_ALIASES * 2]
    _save(data)
    return {"topic": key, "added": new}
