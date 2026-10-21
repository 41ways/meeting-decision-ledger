"""시연용 대화방·요청 시드.

대화방의 답은 scripts/demo_outputs.json(capture_demo.py가 지금 코드로 서버에 실제로 물어 받은 답)이 있으면 그것을,
없으면 아래 상수(예전 실제 실행 결과를 옮겨 적은 목업)를 쓴다. 시연 화면의 대화방 목록,
관리자 콘솔의 승인 대기·열람 요청, 알림이 비어 있지 않게 하려는 용도이며 평가(eval)에는 쓰지 않는다.

사용: .venv/bin/python scripts/seed_demo.py   (reset_demo.sh가 자동 실행)
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.session_store import SqliteSessionStore  # noqa: E402
from ledger.access import AccessStore  # noqa: E402
from ledger.store import LedgerStore  # noqa: E402

M04_REPORT = """## OPS-M04 기능 확장 회의 정리 (2026-10-13)
참석 서지후(PL) · 장상민(백엔드) · 노영오(AI 개발) · 한소율(운영팀)

### 회의 요약
주제: 실시간 전송 방식, 운영 일정 연장, 운영 로그 비교
- 웹소켓 전환 필요성이 제기되어 실시간 전송 방식 변경 — 확인 필요 [3, 4, 5, 6]
- 운영 반영 일정 연장을 요청하며 2주 연장 — 확인 필요 [6, 7]
- 운영 로그를 외부 LLM으로 비교하는 방안 논의됨 — 확인 필요 [8, 9]
방향: 결정 4건(확인 필요 3) · 미결 0건 · 할 일 3건

### 결정 (4건, 확인 필요 3건)
1) **실시간 전송 방식을 웹소켓으로 변경** — [확인 필요] 규정 충돌 · 기존 결정과 다름  (결정 서지후)
   근거: [3] 장상민 "멈추려면 화면에서 서버로도 신호를 보내야 하니까 양방향이 필요해요. 실시간 전송을 웹소켓으로 바꾸는 게 깔끔합니다."; [4] 서지후 "그럼 실시간 전송은 웹소켓으로 바꾸는 걸로 하죠."
   - 규정 충돌 [ARC-02] WebSocket 사용
   - 기존 결정과 다름 [OPS-0007] SSE 대신 WebSocket 사용
   - 다른 프로젝트 참고 [CSB-0004] WebSocket 유지
   ▶ 해결 방향
     · ARC-02 예외가 필요하면 아키텍처 위원회 승인
     · 현재 유효 결정 OPS-0007를 바꾸는 결정 — 팀장이 승인하면 OPS-0007가 대체됨. OPS-0007에서 기각했던 대안: WebSocket (CSBOT 프록시 끊김 사례, ARC-02 예외 승인 필요), Long Polling 유지
     · [AI 제안·검토 필요] ARC-02 예외 승인 후 웹소켓 사용
2) **운영 반영 일정을 2주 연장** — [확인 필요] 규정 충돌 · 권한 초과 · 기준 문서와 다름  (결정 서지후)
   근거: [6] 장상민 "근데 웹소켓으로 바꾸면 작업량이 늘어서 일정이 빠듯해요. 운영 반영을 2주 미루면 될 것 같아요."; [7] 서지후 "네 그럼 운영 반영은 2주 미루는 걸로 하겠습니다."
   - 규정 충돌 [OPS-01] CAB 승인 없이 일정 연장
   - 권한 초과 [AUTH-03] 승인 권한자 PM / 본부장 — 마일스톤을 1주 이상 2주 미만 지연하는 결정은 PM 승인, 2주 이상 지연은 본부장 승인이 필요하다.
   - 기준 문서와 다름 [REQ-06] 일정 연장 없이 CAB 승인 후 반영
   ▶ 해결 방향
     · OPS-01 예외가 필요하면 CAB 승인
     · 지금 2주 — 기준 2주 이상이라 본부장 승인 필요. 2주 미만으로 줄이면 PM 승인으로 가능
     · 기준 문서 REQ-06 개정 필요 (요구사항 변경이면 PM 승인)
     · [AI 제안·검토 필요] CAB 승인 후 2주 지연
3) **운영 로그를 외부 LLM으로 비교** — [확인 필요] 규정 충돌 · 권한 초과  (결정 서지후)
   근거: [8] 한소율 "품질 비교할 때 샘플만으로는 부족해서, 실제 운영 로그를 외부 LLM API에 그대로 넣어서 비교하고 싶어요."; [9] 서지후 "보안 예외 승인 받은 게 있으니까 그렇게 진행하죠."
   - 규정 충돌 [SEC-01] 원문 전송 위반
   - 권한 초과 [AUTH-02] 승인 권한자 PM — 요구사항(범위·성능 기준 포함) 변경은 PM 승인이 필요하며, 승인 후 요구사항정의서를 개정해야 한다.
   ▶ 해결 방향
     · 예외 W-001 범위 'PoC 환경에서 마스킹된 운영 로그 샘플 200건을 외부 LLM API로 품질 비교 평가'(~2026-11-30) 안으로 바꾸면 가능
     · SEC-01 예외가 필요하면 보안 담당 승인
     · PM 승인 요청
     · [AI 제안·검토 필요] 마스킹 후 외부 LLM 비교
4) **캐시 적용 범위를 런북 문서로 제한** — [승인 대기] 기존 승인 대기 OPS-0011을 확정하는 발언  (결정 서지후)
   근거: [10] 장상민 "캐시 후보 정리해 왔는데 런북 문서만 캐시하는 게 맞을 것 같아요. 모니터링 수치는 빼고요."; [11] 서지후 "좋습니다. 캐시는 런북 문서에만 적용하고 모니터링 조회는 매번 새로 하는 걸로 하겠습니다."

### 할 일
- 웹소켓 전환 설계 — 담당 장상민 / 기한 2026-10-17 (근거 12, 13)  ⚠ 결정 1이 확인 필요 상태 (규정 충돌 · 기존 결정과 다름)
- 운영 로그로 외부 LLM 비교 — 담당 한소율 / 기한 2026-10-20 (근거 14, 15)  ⚠ 결정 3이 확인 필요 상태 (규정 충돌 · 권한 초과)
- 런북 캐시 적용 — 담당 장상민 / 기한 2026-10-17 (근거 16)

### 눈여겨볼 점
- 팀장(정한결) 불참 회의에서 결정 4건 중 3건이 확인 필요
- '실시간 전송 방식' 사안 3번째 변경 시도: 실시간 진행 표시는 Long Polling으로 시작한다(09-15) → 실시간 전송을 Long Polling에서 SSE로 변경한다(09-29) → 이번 회의
- 할 일 2건이 확인 필요인 결정에 묶여 있음 — 진행 전 확인"""

SSE_ANSWER = """[확정] SSE는 Long Polling보다 사내 프록시 통과가 가능하고, WebSocket의 프록시 연결 끊김 사례를 고려해 선택했다.  
결정 ID: [OPS-0007]

### OPS-0007 · 실시간 전송을 Long Polling에서 SSE로 변경한다
현재 유효 · 시행 2026-09-29 · 결정 서지후

이유
- 단방향이면 충분
- 사내 프록시 통과
- Long Polling은 요청 누적

기각한 대안
- WebSocket (CSBOT 프록시 끊김 사례, ARC-02 예외 승인 필요)
- Long Polling 유지

원문 · OPS-M02 [8] 서지후
> 실시간 전송은 SSE로 바꾸겠습니다. 서버에서 화면으로 한 방향이면 충분하고 사내 프록시도 그냥 통과해요. 웹소켓은 고객센터 챗봇 때 프록시에서 연결이 끊겨서 결국 포기했잖아요.

원문 · OPS-M02 [9] 김태림
> 웹소켓은 아키텍처 원칙상 예외 승인도 받아야 해서 SSE가 낫습니다.

### CSB-0004 · WebSocket을 중단하고 Long Polling으로 전환한다
다른 프로젝트 참고 · 고객센터 챗봇 · 시행 2025-08-20 · 결정 CSBOT PL

이유
- 사내 프록시에서 WebSocket 연결이 주기적으로 끊겨 장애가 반복됨

기각한 대안
- WebSocket 유지

원장 인용 · CSB-M09 (원문은 보존 기한이 지나 파기)
> 프록시에서 웹소켓이 계속 끊겨서 더는 못 쓰겠어요. 롱폴링으로 돌립니다.

### 이전 논의 · 이후 결정으로 이어짐
- OPS-M01 [9] 장상민: 실시간 진행 표시는요? 고객센터 챗봇 때는 롱폴링 썼던 걸로 아는데.

### 이 사안의 흐름
- OPS-0003 [대체됨] 실시간 진행 표시는 Long Polling으로 시작한다 (09-15)
- OPS-M01 (09-15) [미결 → 결정으로 이어짐] 실시간 전송 방식 변경 가능성
- OPS-M01 (09-15) [승인 대기] 실시간 전송 롱폴링으로 시작, 이후 변경 가능 (원장에 올리지 않음)
- OPS-0007 [확정] 실시간 전송을 Long Polling에서 SSE로 변경한다 (09-29)
- OPS-M02 (09-29) [승인 대기] 실시간 전송은 SSE로 변경 (원장에 올리지 않음)
- OPS-M04 (10-13) [확인 필요] 실시간 전송 방식을 웹소켓으로 변경 (원장에 올리지 않음)"""

REDIS_REVIEW = """**검토 결과: 충돌** (주제 session.store)
- 기존 결정과 다름 [OPS-0008] PostgreSQL 사용 필수
- 다른 프로젝트 참고 [CSB-0005] Redis 사용 금지
- 참고: 다른 프로젝트 결정은 참고용: CSB-0005
▶ 해결 방향
  · 현재 유효 결정 OPS-0008를 바꾸는 결정 — 팀장이 승인하면 OPS-0008가 대체됨. OPS-0008에서 기각했던 대안: Redis"""

CSB_ANSWER = """[확정] CSB-0004에 따르면, 고객센터 챗봇에서는 웹소켓을 중단하고 롱폴링으로 전환했다. 이유는 사내 프록시에서 웹소켓 연결이 주기적으로 끊겨 장애가 반복되었기 때문이다.

### CSB-0004 · WebSocket을 중단하고 Long Polling으로 전환한다
현재 유효 · 시행 2025-08-20 · 결정 CSBOT PL

이유
- 사내 프록시에서 WebSocket 연결이 주기적으로 끊겨 장애가 반복됨

기각한 대안
- WebSocket 유지

원장 인용 · CSB-M09 (원문은 보존 기한이 지나 파기)
> 프록시에서 웹소켓이 계속 끊겨서 더는 못 쓰겠어요. 롱폴링으로 돌립니다.

### OPS-0007 · 실시간 전송을 Long Polling에서 SSE로 변경한다
다른 프로젝트 참고 · 운영 장애 대응 AI · 시행 2026-09-29 · 결정 서지후

이유
- 단방향이면 충분
- 사내 프록시 통과
- Long Polling은 요청 누적

기각한 대안
- WebSocket (CSBOT 프록시 끊김 사례, ARC-02 예외 승인 필요)
- Long Polling 유지

원문 · OPS-M02 [8] 서지후
> 실시간 전송은 SSE로 바꾸겠습니다. 서버에서 화면으로 한 방향이면 충분하고 사내 프록시도 그냥 통과해요. 웹소켓은 고객센터 챗봇 때 프록시에서 연결이 끊겨서 결국 포기했잖아요.

원문 · OPS-M02 [9] 김태림
> 웹소켓은 아키텍처 원칙상 예외 승인도 받아야 해서 SSE가 낫습니다.

### 이 사안의 흐름
- CSB-0001 [대체됨] 상담원 화면 실시간 알림은 WebSocket으로 한다 (03-10)
- CSB-0004 [확정] WebSocket을 중단하고 Long Polling으로 전환한다 (08-20)"""

I01_SUMMARY = """## HBE-I01 면접 요약 (2026-10-16)

### 질문·답변 요약
1) **Q** 자기소개 부탁드립니다.  [1]
   **A** 물류 회사에서 4년간 주문 처리 서버를 맡았습니다. 스프링과 PostgreSQL을 사용했습니다.  [2]
2) **Q** 장애 직접 해결 경험  [3]
   **A** 새벽 주문 API 느려짐을 해결해 30분 내 복구. 쿼리 점검 체크리스트 도입.  [4]
3) **Q** 결혼 여부 및 야근 가능 여부  [5]
   **A** 업무와 무관해 답변 어렵습니다. 야근 가능 여부는 말씀드릴 수 있습니다.  [6]
4) **Q** 팀원과 의견 차이 대처  [7]
   **A** 캐시 도입 논의 시 부하 테스트로 수치로 결정. 팀원 의견 수렴 후 결정.  [8]
5) **Q** 고향 및 부모 직업  [9]
   **A** 답변 거부  [10]
6) **Q** 트랜잭션 격리 수준 기준  [11]
   **A** 기본 READ COMMITTED로, 갱신 충돌 시 비관적 락. 격리 수준 상승 시 처리량 감소 때문.  [12]
7) **Q** 합격 여부  [13]
   **A** 답변 거부  [—]

### 공정성 점검
- ⚠ [5] 서지후 "결혼은 하셨어요? 저희가 야근이 좀 있어서요."
  - policy [FAIR-01] 혼인 여부 개인 정보 질문
- ⚠ [9] 서지후 "고향이 어디세요? 부모님은 무슨 일 하세요?"
  - policy [FAIR-01] 가족 정보 질문
- ⚠ [13] 서지후 "좋네요. 이분은 그냥 합격으로 하죠."
  - policy [FAIR-03] 면접 중 개인이 합격 확정"""

CACHE_ANSWER = """[승인 대기] 캐시는 런북 문서에만 적용하기로 했다. 결정 ID: [OPS-0011]

### OPS-0011 · 캐시 적용 범위를 런북 문서로 제한
승인 대기 · 아직 권위 없음 · 결정 서지후

이유
- 모니터링 수치는 계속 바뀌어 캐시하면 옛 값이 나옴

기각한 대안
- 자주 묻는 질문 Redis 캐시

원문 · OPS-M04 [10] 장상민
> 캐시 후보 정리해 왔는데 런북 문서만 캐시하는 게 맞을 것 같아요. 모니터링 수치는 빼고요.

원문 · OPS-M04 [11] 서지후
> 좋습니다. 캐시는 런북 문서에만 적용하고 모니터링 조회는 매번 새로 하는 걸로 하겠습니다.

### 이전 논의 · 이후 결정으로 이어짐
- OPS-M03 [10] 서지후: 캐시는 대상부터 정해야 할 것 같아요. 오늘 결론 내지 말고 상민 님이 후보 정리해서 다음에 다시 봅시다."""

OUTPUTS_FILE = ROOT / "scripts" / "demo_outputs.json"
CAPTURED = json.loads(OUTPUTS_FILE.read_text(encoding="utf-8")) if OUTPUTS_FILE.exists() else {}


def ans(key: str, fallback: str) -> str:
    """
    대화방 답 — 캡처한 실제 답이 있으면 그것, 없으면 목업 상수
    Args:
        key:      demo_outputs.json의 키 (capture_demo.SPECS)
        fallback: 목업 상수
    Returns:
        답 문장
    """
    return CAPTURED.get(key, {}).get("reply") or fallback


# (thread_id, user, 제목, 프로젝트, 회의, 몇 시간 전, [(질문, 답)])
M04, SSE, CACHE = ans("m04", M04_REPORT), ans("sse", SSE_ANSWER), ans("cache", CACHE_ANSWER)
THREADS = [
    ("demo-1", "kjh", "OPS-M04 회의 정리해줘", "OPS", "OPS-M04", 0.3, [("OPS-M04 회의 정리해줘", M04)]),
    ("demo-2", "kjh", "SSE는 왜 골랐어?", "OPS", "OPS-M02", 1.5, [("SSE는 왜 골랐어?", SSE)]),
    ("demo-3", "kjh", "HBE-I01 면접 요약해줘", "HBE", "HBE-I01", 3, [("HBE-I01 면접 요약해줘", ans("i01", I01_SUMMARY))]),
    ("demo-4", "kjh", "세션 저장소를 Redis로 바꾸면 어때?", "OPS", None, 26,
     [("세션 저장소를 Redis로 바꾸면 어때?", ans("redis_session", REDIS_REVIEW))]),
    ("demo-5", "kjh", "예전 고객센터 챗봇에서 웹소켓을 그만둔 이유", "CSB", None, 30,
     [("예전 고객센터 챗봇에서는 왜 웹소켓을 그만뒀어요?", ans("csb", CSB_ANSWER))]),
    # 시연에서 이 방에 '이거 누가 승인했어?'를 직접 입력한다 (사실 질문은 모델 없이 표에서 바로 답함)
    ("demo-6", "kjh", "캐시는 어떻게 하기로 했어?", "OPS", "OPS-M03", 75, [("캐시는 어떻게 하기로 했어?", CACHE)]),
    ("demo-7", "pdy", "OPS-M04 회의 정리해줘", "OPS", "OPS-M04", 0.5, [("OPS-M04 회의 정리해줘", M04)]),
    ("demo-8", "pdy", "SSE는 왜 골랐어?", "OPS", "OPS-M02", 20, [("SSE는 왜 골랐어?", SSE)]),
    ("demo-9", "lsj", "캐시는 어떻게 하기로 했어?", "OPS", "OPS-M03", 2, [("캐시는 어떻게 하기로 했어?", CACHE)]),
]
if "redis_cache" in CAPTURED:      # 용어 색인 검색 예시 (미결 답) — 캡처한 답이 있을 때만
    THREADS.append(("demo-10", "kjh", "Redis 캐시 쓰기로 했어?", "OPS", None, 1,
                    [("Redis 캐시 쓰기로 했어?", CAPTURED["redis_cache"]["reply"])]))


def main():
    """
    시연용 대화방·승인 대기 결정·열람 요청을 넣는다

    access.db의 모든 행과 session.db의 demo-* 대화를 지운 뒤 다시 넣는다. 원장(LedgerStore)은 비우지 않는다.
    """
    acc, sess, ledger = AccessStore(), SqliteSessionStore(), LedgerStore()
    acc.reset()
    with sqlite3.connect(ROOT / "session.db") as c:
        c.execute("DELETE FROM messages WHERE thread_id LIKE 'demo-%'")
    now = datetime.now()
    for tid, uid, title, key, mid, hours_ago, turns in THREADS:
        for q, a in turns:
            sess.append(tid, "user", q)
            sess.append(tid, "assistant", a)
        acc.touch_thread(tid, uid, title, key, mid)
        if tid in ("demo-6", "demo-9"):           # 이어 묻기('이거 누가 승인했어?')가 실제로 동작하게 직전 답의 대상을 넣는다
            acc.set_thread_refs(tid, ["OPS-0011", "OPS-M04"])
        # touch_thread는 갱신 시각을 지금으로 찍는다. 목록에 시간차가 보이게 THREADS의 '몇 시간 전'으로 되돌린다
        acc.conn.execute("UPDATE threads SET updated=? WHERE thread_id=?",
                         ((now - timedelta(hours=hours_ago)).isoformat(timespec="seconds"), tid))
    acc.conn.commit()

    # 승인 대기 1건 (서지후가 M04 정리에서 올린 런북 캐시 결정) — 원장에는 proposed로
    m04 = {s["seq"]: s for s in json.loads((ROOT / "data/project/r1_meetings/OPS-M04.json").read_text())["segments"]}
    # 이미 cache 주제 결정이 있으면 다시 올리지 않는다 (재실행 시 중복 방지). 요청 기록은 acc.reset 뒤라 매번 넣는다
    cached = [d for d in ledger.decisions(project_id="PRJ-OPSAI") if d["topic"] == "cache"]
    did = cached[0]["decision_id"] if cached else None
    if not did:
        did = ledger.propose({"project_id": "PRJ-OPSAI", "topic": "cache", "statement": "캐시 적용 범위를 런북 문서로 제한",
                              "rationale": "모니터링 수치는 계속 바뀌어 캐시하면 옛 값이 나옴",
                              "alternatives_rejected": ["자주 묻는 질문 Redis 캐시"],
                              "decided_by": "서지후",
                              "evidence": [{"meeting_id": "OPS-M04", "seq": q, "quote": m04[q]["text"]}
                                           for q in (10, 11)]})
    if cached and cached[0]["status"] != "proposed":
        did = None                                  # 이미 승인·반려된 원장이면 승인 대기 시드는 건너뛴다
    if did:
        acc.record_proposal(did, "pdy", "OPS-M04")
        # 회의 기록(process_meetings.py가 채운 OPS-M04 결정 후보)에 이 원장 번호를 잇는다 — 사안 흐름·미결 연결이 원장을 가리키게
        from ledger.records import MeetingRecords  # pylint: disable=import-outside-toplevel
        rec = MeetingRecords()
        if rec.item("OPS-M04:decision:11"):
            rec.link_decision("OPS-M04:decision:11", did, "서지후")
    # 열람 요청 1건 (인턴이 참석하지 않은 회의)
    acc.request_access("yhr", "OPS-M04", "온보딩 자료 정리")
    src = f"캡처 답 {len(CAPTURED)}개(demo_outputs.json)" if CAPTURED else "목업 상수"
    print(f"시드 완료: 대화방 {len(THREADS)}개 ({src}), 승인 대기 {1 if did else 0}건, 열람 요청 1건")


if __name__ == "__main__":
    main()
