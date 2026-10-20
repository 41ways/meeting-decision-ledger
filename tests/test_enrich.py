"""회의 정리 보강(라벨·용어 통일·칸 기록·해결 제안·근거 요약·인사이트) 단위 테스트."""
# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument  # 가짜 객체 관용
import asyncio

from agent import enrich as E


def run(coro):
    return asyncio.run(coro)


def test_labels_are_only_check_or_pending_for_meeting_items():
    assert E.label_for({"status": "conflict", "conflicts": [{"type": "policy"}, {"type": "authority"}]}) == \
        ("확인 필요", "규정 충돌 · 권한 초과")
    assert E.label_for({"status": "needs_approval", "notes": ["주제 불확실"]}) == ("확인 필요", "주제 불확실")
    assert E.label_for({"status": "no_basis"})[0] == "승인 대기"
    assert E.label_for({"status": "ok", "confirms": ["OPS-0010"]})[1].startswith("기존 승인 대기 OPS-0010")


GLOSSARY = """| 표준 용어 | 뜻 | STT 오인식 예 |
|---|---|---|
| 권위 게이트 (Authoritative Gate) | 검사 | 버니게이트, 머니게이트 |
| 런북 (Runbook) | 절차 문서 | 런붐, 럼북 |"""


def test_glossary_normalizes_stt_errors():
    gl = E.parse_glossary(GLOSSARY)
    assert gl[0] == ("권위 게이트", ["버니게이트", "머니게이트"])
    assert E.normalize("버니게이트로 걸러요, 런붐만 캐시", gl) == "권위 게이트로 걸러요, 런북만 캐시"


class SlotLLM:
    async def json(self, system, user, schema, **kw):
        return {"items": [{"i": 0, "target": "최종 답변 시간", "change": "30초 → 60초"},
                          {"i": 1, "target": "운영 반영 일정", "change": "3주 연기", "scope": "전체"}]}, {"tokens": 1}


def test_slots_keep_only_values_in_evidence():
    items = [{"statement": "최종 답변 기준을 60초로 변경",
              "evidence": [{"quote": "최종 답변 기준을 60초로 늘리겠습니다. 지금은 30초예요."}]},
             {"statement": "운영 반영을 2주 연기", "evidence": [{"quote": "운영 반영은 2주 미루는 걸로 하겠습니다."}]}]
    run(E.fill_slots(SlotLLM(), items, []))
    assert items[0]["slots"]["change"] == "30초 → 60초" and items[0]["canonical"] == "최종 답변 시간: 30초 → 60초"
    assert items[1]["slots"]["change"] == "" and "근거에 없는 값 3주" in items[1]["slot_warn"][0]   # 지어낸 숫자는 버린다


def test_authority_suggestion_compares_threshold_numerically():
    item = {"statement": "운영 반영을 2주 연기", "evidence": [{"quote": "2주 미루는 걸로 하겠습니다."}],
            "review": {"status": "conflict", "conflicts": [{"type": "authority", "against": ["AUTH-03"], "detail": ""}]},
            "authority_check": {"ok": False, "approver": "PM / 본부장", "threshold": "2주"}, "research": {}}
    text = E.suggest(item)[0]["text"]
    assert "기준 2주 이상이라 본부장 승인" in text and "2주 미만으로 줄이면 PM 승인" in text


def test_policy_and_past_decision_suggestions_come_from_tables():
    item = {"statement": "운영 로그를 외부 LLM으로 비교", "evidence": [],
            "review": {"status": "conflict", "conflicts": [
                {"type": "policy", "against": ["SEC-01"], "detail": ""},
                {"type": "past_decision", "against": ["OPS-0007"], "detail": ""}]},
            "waivers": [{"waiver_id": "W-001", "rule_id": "SEC-01", "scope": "마스킹 샘플 PoC", "expires_at": "2026-11-30"}],
            "research": {"rules": [{"rule_id": "SEC-01", "approver_role": "보안 담당"}],
                         "current": {"decision_id": "OPS-0007", "alternatives_rejected": ["WebSocket"]}}}
    texts = [s["text"] for s in E.suggest(item)]
    assert any("W-001" in t and "마스킹 샘플 PoC" in t for t in texts)
    assert any("보안 담당 승인" in t for t in texts)
    assert any("OPS-0007가 대체됨" in t and "기각했던 대안: WebSocket" in t for t in texts)
    assert all(not s["ai"] for s in E.suggest(item))


class SummaryLLM:
    async def json(self, system, user, schema, **kw):
        return {"topic": "멈춤 기능·일정", "direction": "확인 필요 3건",
                "lines": [{"text": "멈춤에 양방향이 필요해 WebSocket 논의", "seqs": [3, 4]},
                          {"text": "근거 없는 줄", "seqs": []},
                          {"text": "운영 반영 3주 연기", "seqs": [7]},
                          {"text": "없는 발언", "seqs": [99]}]}, {"tokens": 1}


def test_summary_keeps_only_lines_with_valid_evidence():
    segs = [{"seq": 3, "speaker": "a", "role": "x", "text": "양방향이 필요해요"},
            {"seq": 4, "speaker": "b", "role": "PL", "text": "웹소켓으로 바꾸죠"},
            {"seq": 7, "speaker": "b", "role": "PL", "text": "2주 미루는 걸로"}]
    held = [{"decided_seq": 4, "statement": "웹소켓", "label": "확인 필요"}]
    sm, _ = run(E.write_summary(SummaryLLM(), segs, held, [], []))
    assert [ln["seqs"] for ln in sm["lines"]] == [[3, 4]] and sm["dropped"] == 3
    assert sm["lines"][0]["text"].endswith("— 확인 필요") and sm["direction"] == "결정 1건(확인 필요 1) · 미결 0건 · 할 일 0건"


def test_insights_and_action_ties():
    items = [{"statement": "실시간 전송을 웹소켓으로 변경", "canonical": "실시간 전송 방식: SSE → WebSocket",
              "label": "확인 필요", "label_reason": "규정 충돌", "topic": "realtime.transport", "slots": {}}]
    actions = [{"statement": "웹소켓 전환 설계"}, {"statement": "런북 캐시 적용"}]
    E.tie_actions(actions, items)
    assert "결정 1이 확인 필요" in actions[0]["warn"] and "warn" not in actions[1]
    history = [{"topic": "realtime.transport", "status": "approved", "statement": "롱폴링", "effective_from": "2026-09-15"},
               {"topic": "realtime.transport", "status": "approved", "statement": "SSE", "effective_from": "2026-09-29"}]
    meta = {"date": "2026-10-13", "attendees": [{"name": "서지후"}]}
    out = E.insights(meta, items, actions, history, [{"meeting_id": "OPS-M03", "date": "2026-10-06", "statement": "캐시 대상"}])
    assert any("불참 회의에서 결정 1건 중 1건이 확인 필요" in x for x in out)
    assert any("3번째 변경 시도" in x for x in out)
    assert any("할 일 1건이 확인 필요" in x for x in out)
    assert any("이전 미결 해소: OPS-M03" in x for x in out)


class TopicLLM:
    async def json(self, system, user, schema, **kw):
        return {"items": [{"i": 0, "topic": "cache"}, {"i": 1, "topic": "made.up"}]}, {"tokens": 1}


def test_open_topics_only_from_list():
    und = [{"subject": "캐시 대상", "evidence": [{"quote": "캐시는 대상부터"}]},
           {"subject": "기타", "evidence": [{"quote": "x"}]}]
    run(E.open_topics(TopicLLM(), und, "cache: 캐시\nllm.tiering: 티어링"))
    assert [u["topic"] for u in und] == ["cache", ""]          # 목록에 없는 키는 버린다 (잇지 않음)


def test_alternative_repeating_the_conflicting_change_is_dropped():
    d = {"slots": {"change": "SSE → WebSocket"}}
    assert E._repeats_change("WebSocket 사용을 유지하고 전송 방식 변경", d)
    assert not E._repeats_change("WebSocket 대신 SSE 유지, 멈춤은 별도 POST 요청", d)
