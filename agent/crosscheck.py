"""교차검증 — 8b 판단에 작은 모델 2개 × 질문 방식 2가지로 동의·이의를 받는다.

판단을 내리는 건 8b이고, 작은 모델은 동의/이의만 한다(작은 모델이 혼자 답을 정하지 않는다).
- 4/4 동의: 그대로 진행
- 이의 1표: 8b 재심 — 이의가 있었다는 사실을 보여 주고 다시 묻는다. 유지하면 진행, 바뀌면 '확인 필요'
- 이의 2표 이상: '확인 필요' (근거와 함께 사람에게)
이의가 결과를 자동으로 뒤집지 않는다. 뒤집힌 판단이 맞다는 보장도 없기 때문이다.

검증하는 곳은 틀렸을 때 피해가 큰 방향의 판단만이다.
- 결정인가: 8b가 '결정이다'라고 한 항목 (원장에 잘못 올라가는 방향)
- 기준과 충돌하나: 8b가 '같다·무관하다'라고 한 정책·결정·기준 문서 (위반을 놓치는 방향)
- 근거 질문: 고른 결정이 질문에 답하는가 (엉뚱한 결정으로 답하는 방향)

환경변수 CROSSCHECK=1일 때만 켠다 (실험에서 켠 것과 끈 것을 비교하려고).
"""
from __future__ import annotations

import asyncio
import os

CHECK_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
                "required": ["answer"]}
SMALL_CTX = 4096          # 짧은 판정이라 컨텍스트를 줄여 작은 모델 여러 개를 함께 띄울 메모리를 남긴다
SYSTEM = "주어진 질문에 yes 또는 no로만 답한다. 근거 발언과 기준 문장에 쓰인 내용만 보고 판단한다."


def enabled() -> bool:
    """
    교차검증을 켰는지 (CROSSCHECK=1)
    Returns:
        켰으면 True
    """
    return os.environ.get("CROSSCHECK", "0") == "1"


def build_checkers() -> list:
    """
    교차검증용 작은 모델 2개 (계열이 다른 두 모델 — 같은 방향으로 같이 틀리는 경우를 줄이려고)
    Returns:
        LocalLLM 목록. 꺼져 있으면 빈 목록
    """
    if not enabled():
        return []
    from ledger.llm import LocalLLM  # pylint: disable=import-outside-toplevel  # 꺼져 있을 땐 불러오지 않는다
    names = os.environ.get("CROSSCHECK_MODELS", "qwen3:0.6b,qwen2.5:0.5b").split(",")
    return [LocalLLM(n.strip(), label="checker") for n in names if n.strip()]


async def _ask(llm, prompt: str) -> tuple[str | None, dict | None]:
    """
    작은 모델에 yes/no 한 번 묻기 (실패하면 None — 기권으로 센다)
    Args:
        llm:    작은 모델
        prompt: 질문과 근거
    Returns:
        ("yes" | "no" | None, 사용량)
    """
    try:
        out, usage = await llm.json(SYSTEM, prompt, CHECK_SCHEMA, max_tokens=8, num_ctx=SMALL_CTX)
        return out.get("answer"), usage
    except Exception:  # pylint: disable=broad-exception-caught  # 검증자 하나가 실패해도 본 판단은 계속한다
        return None, None


async def votes(checkers: list, context: str, forward: str, inverse: str) -> tuple[list[bool | None], list]:
    """
    4표 받기 — 모델 2개 × (정방향 질문, 뒤집은 질문), 동시에

    정방향 질문은 8b 판단이 맞으면 yes, 뒤집은 질문은 맞으면 no가 나오게 짓는다.
    같은 질문을 두 번 하면 온도 0에선 같은 답이 나와 검증이 안 되므로 질문 자체를 뒤집는다.
    Args:
        checkers: 작은 모델 목록
        context:  근거 발언·기준 등 판단 재료
        forward:  8b 판단이 맞으면 yes인 질문
        inverse:  8b 판단이 맞으면 no인 질문
    Returns:
        (동의 여부 목록 — True 동의, False 이의, None 기권, 사용량 목록)
    """
    jobs = []
    for llm in checkers:
        jobs.append(_ask(llm, f"{context}\n\n질문: {forward}"))
        jobs.append(_ask(llm, f"{context}\n\n질문: {inverse}"))
    res = await asyncio.gather(*jobs)
    agree = []
    for n, (ans, _) in enumerate(res):
        if ans is None:
            agree.append(None)
        else:
            agree.append(ans == "yes" if n % 2 == 0 else ans == "no")
    return agree, [u for _, u in res if u]


def verdict(agree: list[bool | None]) -> str:
    """
    표 결과 → 처리
    Args:
        agree: votes()의 동의 여부 목록
    Returns:
        'keep'(모두 동의) / 'rejudge'(이의 1표) / 'defer'(이의 2표 이상)
    Examples:
        verdict([True, True, True, True]) -> 'keep'
        verdict([True, False, True, None]) -> 'rejudge'
    """
    dissent = sum(a is False for a in agree)
    if dissent == 0:
        return "keep"
    return "rejudge" if dissent == 1 else "defer"


async def rejudge(llm, context: str, forward: str) -> tuple[bool, dict | None]:
    """
    8b 재심 — 다른 검토자 한 명이 반대로 봤다는 걸 알리고 같은 질문을 다시 묻는다
    Args:
        llm:     8b (quality_llm)
        context: 판단 재료
        forward: 8b 원래 판단이 맞으면 yes인 질문
    Returns:
        (원래 판단 유지 여부, 사용량)
    """
    out, usage = await llm.json(
        SYSTEM + " 다른 검토자 한 명이 반대로 판단했다. 근거 발언과 기준 문장을 다시 읽고 답한다.",
        f"{context}\n\n질문: {forward}", CHECK_SCHEMA, max_tokens=8)
    return out.get("answer") == "yes", usage


async def check(deps, kind: str, context: str, forward: str, inverse: str) -> dict:
    """
    판단 하나를 교차검증
    Args:
        deps:    의존성 (checkers, quality_llm)
        kind:    판단 종류 (decision · relation · pick) — 기록용
        context: 판단 재료
        forward: 8b 판단이 맞으면 yes인 질문
        inverse: 8b 판단이 맞으면 no인 질문
    Returns:
        {"kind", "votes", "outcome": keep | keep_after_rejudge | defer, "usage": [...]}
    """
    agree, usage = await votes(deps.checkers, context, forward, inverse)
    v = verdict(agree)
    if v == "rejudge":
        held, u = await rejudge(deps.quality_llm, context, forward)
        usage.append(u)
        v = "keep_after_rejudge" if held else "defer"
    return {"kind": kind, "votes": agree, "outcome": v, "usage": [x for x in usage if x]}


# ---------------------------------------------------------------- 판단별 질문
def decision_job(item: dict, by_seq: dict) -> tuple[str, str, str]:
    """
    '결정인가' 검증 질문 (8b는 '결정이다'라고 했음)
    Args:
        item:   gate를 통과한 결정 후보 (decided_seq, statement)
        by_seq: 발언 번호 → 발언
    Returns:
        (재료, 정방향 질문, 뒤집은 질문)
    """
    q = item["decided_seq"]
    ctx = "\n".join(f"[{s}] {by_seq[s]['speaker']}: {by_seq[s]['text']}" for s in range(q - 2, q + 1) if s in by_seq)
    return (f"회의 발언:\n{ctx}\n\n요약: {item['statement']}",
            f"[{q}] 발언에서 이 내용이 회의에서 확정되었나?",
            f"[{q}] 발언은 아직 정하지 않은 제안·질문·회상·보류인가?")


def relation_job(item: dict, src: dict) -> tuple[str, str, str]:
    """
    '기준과 충돌하지 않나' 검증 질문 (8b는 같다·무관하다고 했음)
    Args:
        item: 검토 항목 (statement, evidence)
        src:  기준 (ref, kind, text)
    Returns:
        (재료, 정방향 질문, 뒤집은 질문)
    """
    said = " / ".join(e.get("quote", "") for e in item.get("evidence", []))
    return (f"검토 대상: {item['statement']}" + (f"\n원문: {said}" if said else "") + f"\n\n기준 {src['ref']}: {src['text']}",
            f"검토 대상이 기준 {src['ref']}를 지키거나 이 기준과 상관없는가?",
            f"검토 대상이 기준 {src['ref']}를 어기거나 이 기준과 다른 방향인가?")


def pick_job(question: str, decision: dict) -> tuple[str, str, str]:
    """
    '고른 결정이 질문에 답하나' 검증 질문
    Args:
        question: 사용자 질문
        decision: 고른 결정 (decision_id, statement)
    Returns:
        (재료, 정방향 질문, 뒤집은 질문)
    """
    return (f"사용자 질문: {question}\n결정 {decision['decision_id']}: {decision['statement']}",
            "이 결정이 사용자 질문에 직접 답하는가?",
            "이 결정은 사용자 질문과 다른 사안에 대한 것인가?")
