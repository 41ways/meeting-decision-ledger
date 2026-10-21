"""회의 정리 보강 — 칸 기록·용어 통일·해결 제안·근거 달린 요약·상태 라벨·인사이트 (docs/설계_재구성안.md §2.2, §3.2).

판정(충돌 종류)이 끝난 뒤에 붙는 단계라 기존 판정 결과를 바꾸지 않는다.
LLM은 칸 채우기·요약 문장·대안 문장만 쓰고, 코드가 원문과 대조해 근거 없는 값은 버린다.
해결 제안 중 승인 권한자·예외·기존 결정은 코드가 표에서 꺼내고, LLM 대안은 'AI 제안'으로 표시한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ledger import aliases
from ledger.aliases import parse_glossary  # noqa: F401  pylint: disable=unused-import  # 용어집 해석은 별칭 색인과 한곳에서

PIPELINE_VERSION = "2026-10-21.1"    # 주제 배정: 별칭 일치 먼저
_USERS = Path(__file__).resolve().parent.parent / "data" / "project" / "users.json"
LEADS = [u["name"] for u in json.loads(_USERS.read_text(encoding="utf-8"))["users"] if u["role"] == "팀장"] \
    if _USERS.exists() else []

# 숫자+단위 — 칸·요약·대안에 쓴 값이 근거 발언에 있는지 글자로 대조한다
VALUE = re.compile(r"\d+(?:\.\d+)?\s?(?:초|분|시간|일|주|개월|년|건|%|원|명|회|배)")
CONFLICT_KO = {"policy": "규정 충돌", "authority": "권한 초과", "past_decision": "기존 결정과 다름",
               "baseline": "기준 문서와 다름", "past_project": "다른 프로젝트 참고"}


# ---------------------------------------------------------------- 상태 라벨
def label_for(review: dict) -> tuple[str, str]:
    """
    판정 결과를 사용자에게 보일 라벨과 이유로 바꾼다

    회의 결정 후보는 '확인 필요' 또는 '승인 대기' 둘 중 하나다 (확정은 팀장 승인 뒤 원장에서만).
    Args:
        review: decide()·가드가 낸 review
    Returns:
        (라벨, 이유)
    Examples:
        label_for({"status": "conflict", "conflicts": [{"type": "policy"}]}) -> ("확인 필요", "규정 충돌")
    """
    s = review.get("status")
    if s == "conflict":
        why = [CONFLICT_KO.get(c["type"], c["type"]) for c in review.get("conflicts", [])
               if c["type"] != "past_project"]
        return "확인 필요", " · ".join(dict.fromkeys(why))
    if s == "needs_approval":
        return "확인 필요", (review.get("notes") or ["사람 확인 필요"])[0]
    if review.get("confirms"):
        return "승인 대기", f"기존 승인 대기 {', '.join(review['confirms'])}을 확정하는 발언"
    if s == "change_approved":
        return "승인 대기", "기준 변경 (권한자 결정)"
    if s == "no_basis":
        return "승인 대기", "비교할 기준 없음 (새 주제)"
    return "승인 대기", "문제없음"


# ---------------------------------------------------------------- 용어 통일
def normalize(text: str, glossary: list[tuple[str, list[str]]]) -> str:
    """
    오인식 표기를 용어집 표준 표기로 바꾼다 ('버니게이트' → '권위 게이트')
    Args:
        text:     바꿀 문장
        glossary: parse_glossary 결과
    Returns:
        바꾼 문장
    """
    for std, variants in glossary:
        for v in variants:
            text = text.replace(v, std)
    return text


def values_ok(text: str, source: str) -> list[str]:
    """
    문장 속 숫자+단위 중 근거에 없는 값
    Args:
        text:   검사할 문장
        source: 근거 글
    Returns:
        근거에 없는 값 목록 (비었으면 통과)
    """
    src = source.replace(" ", "")
    return [v for v in VALUE.findall(text or "") if v.replace(" ", "") not in src]


# ---------------------------------------------------------------- 칸 기록
SLOT_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "i": {"type": "integer"}, "target": {"type": "string"}, "change": {"type": "string"},
    "scope": {"type": "string"}, "condition": {"type": "string"}}, "required": ["i", "target", "change"]}}},
    "required": ["items"]}

SLOT_SYSTEM = (
    "결정마다 칸을 채운다. 근거 발언에 있는 내용만 쓰고, 없으면 빈 문자열로 둔다.\n"
    "- target: 무엇에 대한 결정인가 (명사구, 15자 이내, 용어집 표준 표기. 예: '실시간 전송 방식', '최종 답변 시간')\n"
    "- change: 무엇을 어떻게 (바뀌기 전과 후가 발언에 있으면 '전 → 후'. 예: '30초 → 60초', 'SSE → WebSocket')\n"
    "- scope: 적용 범위 (예: '런북 문서만')\n"
    "- condition: 조건·예외 (예: '모니터링 수치는 제외')")


async def fill_slots(llm, items: list[dict], glossary: list[tuple[str, list[str]]]) -> dict | None:
    """
    결정 후보마다 칸(대상·변경·범위·조건)을 채우고 근거와 대조

    칸 값에 근거 발언에 없는 숫자가 있으면 그 칸을 비우고 '요약 확인 필요'를 남긴다.
    결정 문장 자체에 근거 없는 숫자가 있어도 같은 표시를 남긴다 (예: '최종 답변' 60초 → '응답 시간' 60초).
    Args:
        llm:      칸 채우기에 쓸 모델 (json 메서드)
        items:    결정 후보 (statement, evidence 포함) — slots, canonical, slot_warn을 붙인다
        glossary: parse_glossary 결과
    Returns:
        LLM 사용량 (항목이 없으면 None)
    """
    if not items:
        return None
    rows = "\n".join(f"[{n}] 결정: {d['statement']}\n    근거: " + " / ".join(e["quote"] for e in d["evidence"])
                     for n, d in enumerate(items))
    out, usage = await llm.json(SLOT_SYSTEM, rows, SLOT_SCHEMA, max_tokens=600)
    got = {x.get("i"): x for x in out.get("items", []) if isinstance(x.get("i"), int)}
    for n, d in enumerate(items):
        src = " ".join(e["quote"] for e in d["evidence"])
        raw = got.get(n, {})
        slots, warn = {}, []
        for k in ("target", "change", "scope", "condition"):
            v = normalize((raw.get(k) or "").strip(), glossary)
            if bad := values_ok(v, src):
                warn.append(f"{k}: 근거에 없는 값 {', '.join(bad)}")
                v = ""
            slots[k] = v
        d["statement"] = normalize(d["statement"], glossary)
        if bad := values_ok(d["statement"], src):
            warn.append(f"결정 문장: 근거에 없는 값 {', '.join(bad)}")
        d["slots"] = slots
        # 검색·답변용 정해진 틀의 문장 — 칸이 비면 결정 문장을 그대로
        d["canonical"] = (f"{slots['target']}: {slots['change']}" + (f" ({slots['scope']})" if slots["scope"] else "")
                          if slots["target"] and slots["change"] else d["statement"])
        if warn:
            d["slot_warn"] = warn
    return usage


# ---------------------------------------------------------------- 해결 제안
def _num(text: str, unit: str) -> float | None:
    """
    문장에서 특정 단위의 첫 숫자
    Args:
        text: 문장
        unit: 단위 (예: '주', '만 원')
    Returns:
        숫자 또는 None
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s?" + re.escape(unit), text or "")
    return float(m.group(1)) if m else None


def suggest(item: dict) -> list[dict]:
    """
    충돌·확인 필요 항목의 해결 방향 (코드가 권한표·예외·원장에서 꺼낸 것만)

    - 권한 초과: 승인 권한자. 권한표에 기준값이 있으면 지금 값과 숫자로 비교해 낮은 승인자로 가는 길도 제시
    - 규정 충돌: 걸린 조항의 승인자, 같은 조항의 유효 예외와 그 범위
    - 기존 결정과 다름: 승인하면 대체되는 결정, 그 결정에서 기각했던 대안
    - 기준 문서와 다름: 문서 개정 필요
    Args:
        item: review·research·authority_check·waivers가 붙은 항목
    Returns:
        [{"text": 제안, "ai": False}]
    """
    r, res, out = item.get("review", {}), item.get("research", {}), []
    said = item.get("statement", "") + " " + " ".join(e.get("quote", "") for e in item.get("evidence", []))
    for c in r.get("conflicts", []):
        if c["type"] == "authority":
            auth = item.get("authority_check") or {}
            approver, thr = auth.get("approver") or "", auth.get("threshold") or ""
            text = f"{approver} 승인 요청"
            unit = re.sub(r"[\d.\s]", "", thr.replace("월", "")) if thr else ""
            if "/" in approver and unit and (lim := _num(thr, unit)) is not None:
                lower, higher = [a.strip() for a in approver.split("/", 1)]
                now = _num(said, unit)
                if now is not None and now >= lim:
                    text = (f"지금 {now:g}{unit} — 기준 {thr} 이상이라 {higher} 승인 필요. "
                            f"{thr} 미만으로 줄이면 {lower} 승인으로 가능")
            out.append({"text": text, "ai": False})
        elif c["type"] == "policy":
            rid = c["against"][0]
            rule = next((x for x in res.get("rules", []) if x.get("rule_id") == rid), {})
            ws = [w for w in item.get("waivers", []) if w.get("rule_id") == rid]
            for w in ws:
                out.append({"text": f"예외 {w['waiver_id']} 범위 '{w.get('scope')}'(~{w.get('expires_at')}) 안으로 바꾸면 가능",
                            "ai": False})
            if rule.get("approver_role"):
                out.append({"text": f"{rid} 예외가 필요하면 {rule['approver_role']} 승인", "ai": False})
        elif c["type"] == "past_decision" and res.get("current"):
            cur = res["current"]
            alts = cur.get("alternatives_rejected") or []
            out.append({"text": f"현재 유효 결정 {cur['decision_id']}를 바꾸는 결정 — 팀장이 승인하면 {cur['decision_id']}가 대체됨"
                        + (f". {cur['decision_id']}에서 기각했던 대안: {', '.join(alts)}" if alts else ""), "ai": False})
        elif c["type"] == "baseline":
            out.append({"text": f"기준 문서 {', '.join(c['against'])} 개정 필요 (요구사항 변경이면 PM 승인)", "ai": False})
    if r.get("status") == "needs_approval" and (res.get("topic_doubt") or res.get("similar_other")):
        out.append({"text": "주제가 맞는지 확인한 뒤 승인 화면에서 주제를 고쳐 승인", "ai": False})
    return list({x["text"]: x for x in out}.values())


ALT_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "i": {"type": "integer"}, "alternative": {"type": "string"}}, "required": ["i", "alternative"]}}},
    "required": ["items"]}


async def ai_alternatives(llm, items: list[dict]) -> dict | None:
    """
    충돌 항목마다 원래 목적을 이루면서 충돌을 피하는 대안 한 줄 (LLM, 'AI 제안'으로 표시)

    대안에 근거 글에 없는 숫자가 있으면 버린다.
    Args:
        llm:   대안 작성 모델
        items: 항목 (review, research, statement, evidence) — suggestions에 추가
    Returns:
        LLM 사용량 (대상이 없으면 None)
    """
    targets = [(n, d) for n, d in enumerate(items) if d.get("review", {}).get("status") == "conflict"]
    if not targets:
        return None
    rows = []
    for n, d in targets:
        # 의견 검토 항목은 근거 발언이 없다 — 의견 문장 자체가 목적
        goal = " / ".join(e["quote"] for e in d.get("evidence", [])) or d["statement"]
        rows.append(f"[{n}] 결정: {d['statement']}\n    목적(근거 발언): " + goal
                    + "\n    지켜야 할 기준:\n" + "\n".join(f"      - {t}" for t in _rule_texts(d)))
    out, usage = await llm.json(
        "충돌이 있는 결정마다, 근거 발언에 드러난 목적은 이루면서 '지켜야 할 기준'을 모두 지키는 대안을 하나씩 40자 이내로 쓴다. "
        "기준이 금지하거나 승인을 요구하는 방식을 그대로 쓰는 것은 대안이 아니다. 새 숫자·기한을 지어내지 않는다. "
        "마땅한 대안이 없으면 빈 문자열.", "\n".join(rows), ALT_SCHEMA, max_tokens=400)
    got = {x.get("i"): (x.get("alternative") or "").strip() for x in out.get("items", [])}
    for n, d in targets:
        alt = got.get(n, "")
        src = (" ".join(e["quote"] for e in d.get("evidence", [])) + " " + d["statement"] + " "
               + " ".join(_rule_texts(d)))
        if alt and not values_ok(alt, src) and not _repeats_change(alt, d):
            d.setdefault("suggestions", []).append({"text": alt, "ai": True})
    return usage


def _rule_texts(d: dict) -> list[str]:
    """
    충돌한 기준의 원문 (정책 조항·현재 결정·기준 문서) — 대안이 무엇을 지켜야 하는지
    Args:
        d: review·research·authority_check가 붙은 항목
    Returns:
        기준 문장 목록
    """
    res, out = d.get("research", {}), []
    for c in d["review"]["conflicts"]:
        ref = c["against"][0]
        if c["type"] == "policy":
            out += [f"{ref}: {r.get('text', '')}" for r in res.get("rules", []) if r.get("rule_id") == ref]
        elif c["type"] == "past_decision" and res.get("current"):
            out.append(f"{ref}(현재 유효 결정): {res['current'].get('statement', '')}")
        elif c["type"] == "authority":
            out.append(f"{ref}: {(d.get('authority_check') or {}).get('reason', '')}")
        elif c["type"] == "baseline":
            out += [f"{ref}: {b.get('text', '')[:120]}" for b in res.get("baseline", [])
                    if b.get("section", "").startswith(ref)]
    return out or [c.get("detail", "") for c in d["review"]["conflicts"]]


NEGATION = re.compile(r"않|없이|대신|말고|제외|유지하지|쓰지|안 ")


def _repeats_change(alt: str, d: dict) -> bool:
    """
    대안이 충돌을 일으킨 바로 그 방식을 다시 쓰는지 (예: WebSocket 충돌에 'WebSocket 유지' 대안)
    Args:
        alt: AI 대안 문장
        d:   칸(slots)이 붙은 항목
    Returns:
        그대로 되풀이하면 True (부정 표현과 함께 쓰면 False)
    """
    change = (d.get("slots") or {}).get("change") or ""
    after = change.split("→")[-1].strip() if "→" in change else ""
    return bool(after) and after.lower() in alt.lower() and not NEGATION.search(alt)


OPEN_TOPIC_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
    "i": {"type": "integer"}, "topic": {"type": "string"}}, "required": ["i", "topic"]}}}, "required": ["items"]}


async def open_topics(llm, undecided: list[dict], topics_text: str) -> tuple[dict | None, list[dict]]:
    """
    미결마다 주제를 정한다 (다음 회의의 결정과 같은 사안으로 이으려면 주제가 있어야 한다)

    별칭 일치(ledger/aliases.choose_topic)를 먼저 보고, 별칭이 없거나 여럿일 때만 LLM 선택을 쓴다.
    둘이 다르면 미결에 topic_llm으로 남긴다 (투표는 하지 않는다). 목록에 없는 키는 버린다(빈 주제 = 잇지 않음).
    Args:
        llm:         주제 선택 모델
        undecided:   미결 (subject, evidence) — topic·topic_by를 붙인다
        topics_text: 주제 목록 (ledger://topics)
    Returns:
        (LLM 사용량, 별칭과 LLM이 다른 미결 [{subject, alias, llm}]) — 미결이 없으면 (None, [])
    """
    if not undecided:
        return None, []
    keys = set(re.findall(r"^-?\s*([a-z][a-z0-9_.]+)", topics_text, re.M))
    rows = "\n".join(f"[{n}] {u['subject']} — " + " / ".join(e["quote"] for e in u["evidence"])
                     for n, u in enumerate(undecided))
    out, usage = await llm.json("미결 논의마다 아래 주제 목록의 키 하나를 고른다. 맞는 키가 없으면 빈 문자열.\n" + topics_text,
                                rows, OPEN_TOPIC_SCHEMA, max_tokens=200)
    got = {x.get("i"): (x.get("topic") or "").split(":")[0].strip() for x in out.get("items", [])}
    disagree = []
    for n, u in enumerate(undecided):
        llm_t = got.get(n, "")
        llm_t = llm_t if llm_t in keys else ""
        ch = aliases.choose_topic(f"{u['subject']} / " + " / ".join(e["quote"] for e in u["evidence"]), llm_t)
        if ch["topic"] not in keys:          # 별칭 주제가 목록에 없으면(목록을 줄여 보낸 경우) LLM 선택으로
            ch = {**ch, "topic": llm_t, "by": "LLM" if llm_t else "", "disagree": False}
        u["topic"], u["topic_by"] = ch["topic"], ch["by"]
        if ch["disagree"]:
            u["topic_llm"] = llm_t
            disagree.append({"subject": u["subject"], "alias": ch["topic"], "llm": llm_t})
    return usage, disagree


# ---------------------------------------------------------------- 근거 달린 요약
SUMMARY_SCHEMA = {"type": "object", "properties": {
    "topic": {"type": "string"},
    "lines": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "seqs": {"type": "array", "items": {"type": "integer"}}},
        "required": ["text", "seqs"]}},
    "direction": {"type": "string"}}, "required": ["topic", "lines", "direction"]}

SUMMARY_SYSTEM = (
    "회의의 논의 흐름을 요약한다. 발언에 나온 내용만 쓰고 추측하지 않는다.\n"
    "- topic: 회의에서 다룬 주제 2~4개를 쉼표로 (25자 이내)\n"
    "- lines: 3~5줄, 각 45자 이내. '확정된 항목'의 문장을 되풀이하지 말고, 그 얘기가 왜 나왔는지·어떤 우려나 반대가 있었는지를 "
    "발언에 있는 대로 쓴다. 줄마다 근거 발언 번호(seqs)를 반드시 단다\n"
    "- direction: 빈 문자열로 둔다")


async def write_summary(llm, segs: list[dict], items: list[dict], actions: list[dict],
                        undecided: list[dict]) -> tuple[dict, dict | None]:
    """
    근거 발언 번호가 달린 회의 요약을 쓰고 코드로 검사

    근거 번호가 없거나 원문에 없는 줄, 근거 발언에 없는 숫자를 쓴 줄은 지운다.
    Args:
        llm:       요약 모델
        segs:      회의 발언
        items:     결정 후보 (label 포함)
        actions:   할 일
        undecided: 미결
    Returns:
        (요약 {topic, lines:[{text, seqs}], direction, dropped}, LLM 사용량)
    """
    by_seq = {s["seq"]: s for s in segs}
    known = "\n".join([f"- 결정 [{d['decided_seq']}] {d['statement']} ({d.get('label', '')})" for d in items]
                      + [f"- 미결 [{u.get('seq')}] {u['subject']}" for u in undecided]
                      + [f"- 할 일 [{a.get('seq')}] {a['statement']}" for a in actions])
    lines = "\n".join(f"[{s['seq']}] {s['speaker']}({s['role']}): {s['text'][:120]}" for s in segs)
    out, usage = await llm.json(SUMMARY_SYSTEM, f"## 확정된 항목\n{known}\n\n## 회의 발언\n{lines}",
                                SUMMARY_SCHEMA, max_tokens=500)
    kept, dropped = [], 0
    for ln in out.get("lines", []):
        seqs = [q for q in ln.get("seqs", []) if q in by_seq]
        src = " ".join(by_seq[q]["text"] for q in seqs)
        if not seqs or not ln.get("text") or values_ok(ln["text"], src):
            dropped += 1                       # 근거가 없거나 근거에 없는 값을 쓴 줄은 버린다
            continue
        text = ln["text"].strip().rstrip(".")
        # 확인 필요인 결정을 요약이 '결정됨'으로 쓰지 않게, 그 결정 발언을 근거로 한 줄엔 상태를 붙인다
        if any(d.get("label") == "확인 필요" and d.get("decided_seq") in seqs for d in items):
            text = re.sub(r"\s*(결정됨|확정됨|결정했음|결정)$", "", text) + " — 확인 필요"
        kept.append({"text": text, "seqs": sorted(set(seqs))})
    # 방향은 모델 문장 대신 코드가 상태를 센다 — 확인 필요를 '확정'으로 쓰는 실수가 생기지 않게
    held = sum(d.get("label") == "확인 필요" for d in items)
    direction = (f"결정 {len(items)}건" + (f"(확인 필요 {held})" if held else "") + f" · 미결 {len(undecided)}건"
                 + f" · 할 일 {len(actions)}건")
    topic = out.get("topic", "").strip()
    topic = ", ".join([t.strip() for t in topic.split(",")][:4])[:40]
    return {"topic": topic, "lines": kept, "direction": direction, "dropped": dropped}, usage


# ---------------------------------------------------------------- 인사이트
def _overlap(a: str, b: str) -> int:
    """
    두 문장이 공유하는 2자 이상 낱말 수 (조사를 뗀 앞 2자로 비교, 흔한 말 제외)
    Args:
        a: 문장
        b: 문장
    Returns:
        공유 낱말 수
    """
    wa = {w[:2] for w in re.findall(r"[가-힣A-Za-z]{2,}", a or "")}
    wb = {w[:2] for w in re.findall(r"[가-힣A-Za-z]{2,}", b or "")}
    return len(wa & wb - {"회의", "결정", "적용", "변경", "운영"})


def tie_actions(actions: list[dict], items: list[dict]) -> None:
    """
    할 일마다 관련된 결정 후보를 찾아, 그 결정이 확인 필요면 경고를 붙인다
    Args:
        actions: 할 일 (warn을 붙인다)
        items:   결정 후보 (label 포함)
    """
    for a in actions:
        # 가장 많이 겹치는 결정 하나에만 잇는다
        scored = [(_overlap(a.get("statement", ""), f"{d['statement']} {d.get('canonical', '')}"), n, d)
                  for n, d in enumerate(items, 1)]
        scored.sort(key=lambda x: -x[0])
        best = scored[0] if scored else (0, 0, None)
        second = scored[1][0] if len(scored) > 1 else 0
        # 겹치는 낱말이 있고 2등보다 확실히 많을 때만 (동점이면 어느 결정인지 모르므로 잇지 않는다)
        if best[0] >= 1 and best[0] > second and best[2].get("label") == "확인 필요":
            a["warn"] = f"결정 {best[1]}이 확인 필요 상태 ({best[2].get('label_reason', '')})"


def insights(meta: dict, items: list[dict], actions: list[dict], history: list[dict],
             resolved: list[dict]) -> list[str]:
    """
    회의 단위로 눈여겨볼 점 (전부 코드 집계)

    - 팀장 불참 회의에서 확인 필요가 나온 비율
    - 같은 사안이 최근 여러 번 바뀜 (원장 이력)
    - 확인 필요 결정에 묶인 할 일
    - 이전 미결 해소
    Args:
        meta:     회의 정보 (date, attendees)
        items:    결정 후보 (label, topic)
        actions:  할 일 (warn)
        history:  같은 프로젝트의 원장 결정 (대체된 것 포함)
        resolved: 이번 회의로 해소되는 이전 미결
    Returns:
        한 줄씩의 관찰 목록
    """
    out = []
    names = {a["name"] for a in meta.get("attendees", [])}
    held = [d for d in items if d.get("label") == "확인 필요"]
    absent = [n for n in LEADS if names and n not in names]
    if absent and held:
        out.append(f"팀장({', '.join(absent)}) 불참 회의에서 결정 {len(items)}건 중 {len(held)}건이 확인 필요")
    elif held:
        out.append(f"결정 {len(items)}건 중 {len(held)}건이 확인 필요")
    date = meta.get("date") or ""
    for d in items:
        past = sorted((h for h in history if h.get("topic") == d.get("topic") and h["status"] == "approved"
                       and (h.get("effective_from") or "") <= date), key=lambda h: h.get("effective_from") or "")
        if len(past) >= 2:
            chain = " → ".join(f"{h['statement'].rstrip('.')}({(h.get('effective_from') or '')[5:]})"
                               for h in past[-3:])
            target = d.get('slots', {}).get('target') or d['topic']
            out.append(f"'{target}' 사안 {len(past) + 1}번째 변경 시도: {chain} → 이번 회의")
    tied = [a for a in actions if a.get("warn")]
    if tied:
        out.append(f"할 일 {len(tied)}건이 확인 필요인 결정에 묶여 있음 — 진행 전 확인")
    for r in resolved:
        out.append(f"이전 미결 해소: {r['meeting_id']}({(r.get('date') or '')[5:]}) '{r['statement']}'")
    return out
