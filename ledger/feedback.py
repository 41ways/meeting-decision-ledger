"""사람이 고친 흔적에서 모델이 놓친 것을 찾는다 — 품질 감시의 되먹임.

반려율·수정률 같은 숫자는 '나빠졌다'까지만 알려 준다. 여기서는 사람이 한 수정 하나하나를
'모델이 왜 틀렸나'로 나눠 고칠 거리로 올린다.

- 주제 놓침: 팀장이 주제를 이미 목록에 있던 주제로 바꿨다 → 별칭이 비어 모델이 못 붙인 것.
  결정 문장의 낱말 중 그 주제의 별칭에 없는 것을 별칭 후보로 낸다 (팀장이 누르면 보태짐)
- 목록 밖 사안: 팀장이 새 주제를 만들었다 → 모델이 '새 주제'로 둔 게 맞았는지 확인
- 문장 수정: 결정 요약을 사람이 고쳤다 → 추출 품질 신호
- 반려: 결정이 아니었거나 틀린 결정 → 사유와 함께
- 조용한 실패: 판정·기준 조회 실패가 '문제없음'으로 넘어가지 않았는지 (확인 필요로 간 건수)

보태진 별칭이 생기면 그 놓침은 '반영됨'으로 표시해 고리가 닫혔는지 본다.
"""
from __future__ import annotations

import json

from . import aliases as A


def _known_aliases(topic: str) -> set[str]:
    """주제의 지금 별칭(정규화 표기)"""
    return {a.key for a in A.aliases() if a.topic == topic}


def alias_candidates(statement: str, topic: str, limit: int = 4) -> list[str]:
    """
    결정 문장에서 그 주제의 별칭에 없는 낱말 (별칭 후보)
    Args:
        statement: 결정 문장
        topic:     사람이 고친 주제
        limit:     최대 개수
    Returns:
        후보 낱말
    Examples:
        alias_candidates('캐시 적용 범위를 런북 문서로 제한', 'cache') -> ['런북', '문서', …] (별칭에 없는 것만)
    """
    have = _known_aliases(topic)
    out = []
    for w in A.content_words(statement):
        if len(w) >= 2 and A._key(w) not in have and w not in out:   # pylint: disable=protected-access
            out.append(w)
    return out[:limit]


def report(events: list[dict], decisions: dict[str, dict], base_topics: set[str]) -> dict:
    """
    놓침 신호 보고서
    Args:
        events:      회의 기록 이력 (records.events())
        decisions:   결정 ID → 결정 (statement 등)
        base_topics: 기본 주제 목록의 키 (팀장이 새로 만든 주제와 구분)
    Returns:
        {"misses": [...], "rejections": [...], "summary": {...}}
    """
    def detail(e):
        d = e.get("detail")
        return json.loads(d) if isinstance(d, str) else (d or {})

    added_topics = {e["target"] for e in events if e["kind"] == "topic_added"}
    alias_added = {}
    for e in events:
        if e["kind"] == "aliases_added":
            alias_added.setdefault(e["target"], set()).update(detail(e).get("added", []))
    misses, rejections = [], []
    for e in events:
        d = detail(e)
        if e["kind"] == "decision_edited":
            ch = d.get("changed", {})
            st = (decisions.get(e["target"]) or {}).get("statement", "")
            if "topic" in ch:
                before, after = ch["topic"].get("before") or "", ch["topic"].get("after") or ""
                if after in added_topics and after not in base_topics:
                    kind, why = "목록 밖 사안", "팀장이 새 주제를 만듦 — 모델이 '새 주제'로 둔 게 맞았는지"
                    cands, done = [], True
                else:
                    kind = "주제 놓침" if not before else "주제 오분류"
                    why = (f"목록에 있던 '{after}'를 모델이 못 붙임" if not before
                           else f"모델은 '{before}', 사람은 '{after}'")
                    cands = alias_candidates(st, after)
                    done = bool(alias_added.get(after))
                misses.append({"kind": kind, "decision_id": e["target"], "statement": st, "before": before,
                               "after": after, "why": why, "alias_candidates": cands,
                               "status": "반영됨" if done else "열림", "at": e["at"], "actor": e.get("actor")})
            if "statement" in ch:
                misses.append({"kind": "문장 수정", "decision_id": e["target"], "statement": st,
                               "before": ch["statement"].get("before", ""), "after": ch["statement"].get("after", ""),
                               "why": "결정 요약을 사람이 고침 — 추출 품질", "alias_candidates": [],
                               "status": "기록", "at": e["at"], "actor": e.get("actor")})
        elif e["kind"] == "decision_rejected":
            rejections.append({"decision_id": e["target"], "reason": d.get("reason") or "(사유 없음)",
                               "statement": (decisions.get(e["target"]) or {}).get("statement", ""),
                               "at": e["at"], "actor": e.get("actor")})
    topic_misses = [m for m in misses if m["kind"] in ("주제 놓침", "주제 오분류")]
    summary = {"주제 놓침": len(topic_misses), "반영됨": sum(m["status"] == "반영됨" for m in topic_misses),
               "목록 밖 사안": sum(m["kind"] == "목록 밖 사안" for m in misses),
               "문장 수정": sum(m["kind"] == "문장 수정" for m in misses), "반려": len(rejections),
               "승인": sum(e["kind"] == "decision_approved" for e in events)}
    return {"misses": misses[::-1], "rejections": rejections[::-1], "summary": summary}
