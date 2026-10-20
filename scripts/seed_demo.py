"""시연용 대화방·요청 시드 (하드코딩 목업).

대화 내용은 실제 그래프 실행 결과를 옮겨 적은 것이다(사람 이름·번호는 현재 체계로). 시연 화면의 대화방 목록,
관리자 콘솔의 승인 대기·열람 요청, 알림이 비어 있지 않게 하려는 용도이며 평가(eval)에는 쓰지 않는다.
실제 결과를 보려면 화면에서 새 대화로 다시 실행하면 된다.

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

M04_REPORT = """## OPS-M04 회의 정리 (2026-10-13)

### 회의 요약
주제: 진단 취소 기능과 실시간 전송 방식, 운영 일정 조정
- 운영팀이 진단 중간 멈춤 버튼을 요청
- 양방향 신호가 필요해 WebSocket 전환을 제안
- 작업량 증가로 운영 반영 2주 연기 논의
- 실제 운영 로그로 외부 LLM 품질 비교 희망
- 캐시는 런북 문서로 범위를 좁힘
방향: 취소 기능은 유지하되 전송 방식·일정·외부 비교는 권한자 승인이 먼저 필요

### 결정
1) **실시간 전송 방식을 WebSocket으로 변경** — 충돌  (결정 서지후)
   근거: [3] 장상민 "멈추려면 화면에서 서버로도 신호를 보내야 하니까 양방향이 필요해요. 실시간 전송을 웹소켓으로 바꾸는 게 깔끔합니다."; [4] 서지후 "그럼 실시간 전송은 웹소켓으로 바꾸는 걸로 하죠."
   - policy [ARC-02] WebSocket은 아키텍처 위원회 예외 승인 필요
   - past_decision [OPS-0007] SSE로 정한 유효 결정과 다름
   - past_project [CSB-0004] 프록시 끊김으로 WebSocket 중단 (참고)
2) **운영 반영 일정을 2주 연기** — 충돌  (결정 서지후)
   근거: [6] 장상민 "근데 웹소켓으로 바꾸면 작업량이 늘어서 일정이 빠듯해요. 운영 반영을 2주 미루면 될 것 같아요."; [7] 서지후 "네 그럼 운영 반영은 2주 미루는 걸로 하겠습니다."
   - authority [AUTH-03] 2주 이상 지연은 본부장 승인
   - baseline [REQ-06] 운영 반영 목표일 12/15
3) **운영 로그 원문을 외부 LLM으로 비교** — 충돌  (결정 서지후)
   근거: [8] 한소율 "품질 비교할 때 샘플만으로는 부족해서, 실제 운영 로그를 외부 LLM API에 그대로 넣어서 비교하고 싶어요."; [9] 서지후 "보안 예외 승인 받은 게 있으니까 그렇게 진행하죠."
   - policy [SEC-01] 운영 로그 원문 외부 전송 금지 (W-001은 마스킹 샘플 한정)
   - authority [AUTH-05] 보안 담당 승인 필요
4) **캐시 적용 범위를 런북 문서로 제한** — 문제없음  (결정 서지후)
   근거: [10] 장상민 "캐시 후보 정리해 왔는데 런북 문서만 캐시하는 게 맞을 것 같아요. 모니터링 수치는 빼고요."; [11] 서지후 "좋습니다. 캐시는 런북 문서에만 적용하고 모니터링 조회는 매번 새로 하는 걸로 하겠습니다."

### 할 일
- WebSocket 전환 설계 — 담당 장상민 / 기한 2026-10-17 (근거 12, 13)
- 운영 로그로 외부 LLM 비교 — 담당 한소율 / 기한 2026-10-20 (근거 14, 15)
- 런북 캐시 적용 — 담당 장상민 / 기한 2026-10-17 (근거 16)"""

SSE_ANSWER = """SSE는 단방향 전송이면 충분하고 사내 프록시를 그대로 통과하기 때문에 골랐습니다 (OPS-0007).

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

### CSB-0004 · WebSocket을 중단하고 Long Polling으로 전환한다
다른 프로젝트 참고 · 고객센터 챗봇 · 시행 2025-08-20 · 결정 CSBOT PL

원장 인용 · CSB-M09 (원문은 보존 기한이 지나 파기)
> 프록시에서 웹소켓이 계속 끊겨서 더는 못 쓰겠어요. 롱폴링으로 돌립니다."""

REDIS_REVIEW = """**검토 결과: 충돌** (주제 session.store)
- past_decision [OPS-0008] 대화 이력은 PostgreSQL에 저장하기로 함
- past_project [CSB-0005] 고객센터 챗봇에서 Redis TTL로 대화 유실 (참고)"""

CSB_ANSWER = """고객센터 챗봇은 사내 프록시에서 WebSocket 연결이 계속 끊겨 Long Polling으로 바꿨습니다 (CSB-0004).

### CSB-0004 · WebSocket을 중단하고 Long Polling으로 전환한다
질문한 프로젝트 · 시행 2025-08-20 · 결정 CSBOT PL

이유
- 사내 프록시에서 WebSocket 연결이 주기적으로 끊겨 장애가 반복됨

기각한 대안
- WebSocket 유지

원장 인용 · CSB-M09 (원문은 보존 기한이 지나 파기)
> 프록시에서 웹소켓이 계속 끊겨서 더는 못 쓰겠어요. 롱폴링으로 돌립니다."""

I01_SUMMARY = """## HBE-I01 면접 요약 (2026-10-16)

### 질문·답변 요약
1) **Q** 자기소개  [1]
   **A** 물류 회사 4년, 주문 처리 서버 담당. 스프링·PostgreSQL 사용  [2]
2) **Q** 장애를 직접 해결한 경험  [3]
   **A** 주문 API 지연을 슬로 쿼리 로그로 추적해 빠진 인덱스를 찾아 30분 안에 복구, 배포 전 실행 계획 점검 추가  [4]
3) **Q** 결혼 여부 (야근 관련)  [5]
   **A** 업무와 무관해 답변 거부, 야근 가능 여부는 답변 가능  [6]
4) **Q** 팀원과 의견이 갈렸을 때  [7]
   **A** 캐시 도입 의견 충돌을 부하 테스트 수치로 결정, 팀원 안을 수용  [8]
5) **Q** 고향·부모님 직업  [9]
   **A** 답변 거부  [10]
6) **Q** 트랜잭션 격리 수준 선택 기준  [11]
   **A** 기본 READ COMMITTED, 재고 차감처럼 충돌 나는 곳만 비관적 락. 격리 수준을 올리면 처리량이 떨어짐  [12]

### 공정성 점검
- ⚠ [5] 서지후 "결혼은 하셨어요? 저희가 야근이 좀 있어서요."
  - policy [FAIR-01] 직무와 무관한 혼인 여부 질문
- ⚠ [9] 서지후 "고향이 어디세요? 부모님은 무슨 일 하세요?"
  - policy [FAIR-01] 출신 지역·부모 직업 질문
- ⚠ [13] 서지후 "좋네요. 이분은 그냥 합격으로 하죠."
  - policy [FAIR-03] 면접 중 개인이 합격 확정"""

CACHE_ANSWER = """캐시는 논의는 있었지만 결정되지 않았습니다. 이후 런북 문서에만 적용하자는 안이 승인 대기 중입니다 (OPS-0011).

### OPS-0011 · 캐시 적용 범위를 런북 문서로 제한
승인 대기 · 아직 권위 없음 · 결정 서지후

원문 · OPS-M04 [11] 서지후
> 좋습니다. 캐시는 런북 문서에만 적용하고 모니터링 조회는 매번 새로 하는 걸로 하겠습니다.

### 원문 논의 · 결정되지 않음
- OPS-M03 [8] 장상민: 자주 묻는 질문은 레디스 캐시에 넣어 두면 빠를 것 같은데요.
- OPS-M03 [9] 노영오: 모니터링 수치는 계속 바뀌어서 캐시하면 옛날 값이 나올 수 있어요.
- OPS-M03 [10] 서지후: 캐시는 대상부터 정해야 할 것 같아요. 오늘 결론 내지 말고 상민 님이 후보 정리해서 다음에 다시 봅시다."""

# (thread_id, user, 제목, 프로젝트, 회의, 몇 시간 전, [(질문, 답)])
THREADS = [
    ("demo-1", "kjh", "OPS-M04 회의 정리해줘", "OPS", "OPS-M04", 0.3, [("OPS-M04 회의 정리해줘", M04_REPORT)]),
    ("demo-2", "kjh", "SSE는 왜 골랐어?", "OPS", "OPS-M02", 1.5, [("SSE는 왜 골랐어?", SSE_ANSWER)]),
    ("demo-3", "kjh", "HBE-I01 면접 요약해줘", "HBE", "HBE-I01", 3, [("HBE-I01 면접 요약해줘", I01_SUMMARY)]),
    ("demo-4", "kjh", "세션 저장소를 Redis로 바꾸면 어때?", "OPS", None, 26, [("세션 저장소를 Redis로 바꾸면 어때?", REDIS_REVIEW)]),
    ("demo-5", "kjh", "예전 고객센터 챗봇에서 웹소켓을 그만둔 이유", "CSB", None, 30, [("예전 고객센터 챗봇에서는 왜 웹소켓을 그만뒀어요?", CSB_ANSWER)]),
    ("demo-6", "kjh", "캐시는 어떻게 하기로 했어?", "OPS", "OPS-M03", 75, [("캐시는 어떻게 하기로 했어?", CACHE_ANSWER)]),
    ("demo-7", "pdy", "OPS-M04 회의 정리해줘", "OPS", "OPS-M04", 0.5, [("OPS-M04 회의 정리해줘", M04_REPORT)]),
    ("demo-8", "pdy", "SSE는 왜 골랐어?", "OPS", "OPS-M02", 20, [("SSE는 왜 골랐어?", SSE_ANSWER)]),
    ("demo-9", "lsj", "캐시는 어떻게 하기로 했어?", "OPS", "OPS-M03", 2, [("캐시는 어떻게 하기로 했어?", CACHE_ANSWER)]),
]


def main():
    """
    시연용 대화방·승인 대기 결정·열람 요청을 넣는다 (하드코딩 목업)

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
        # touch_thread는 갱신 시각을 지금으로 찍는다. 목록에 시간차가 보이게 THREADS의 '몇 시간 전'으로 되돌린다
        acc.conn.execute("UPDATE threads SET updated=? WHERE thread_id=?",
                         ((now - timedelta(hours=hours_ago)).isoformat(timespec="seconds"), tid))
    acc.conn.commit()

    # 승인 대기 1건 (서지후가 M04 정리에서 올린 런북 캐시 결정) — 원장에는 proposed로
    m04 = {s["seq"]: s for s in json.loads((ROOT / "data/project/r1_meetings/OPS-M04.json").read_text())["segments"]}
    # 이미 cache 주제 결정이 있으면 다시 올리지 않는다 (재실행 시 중복 방지)
    if not any(d["topic"] == "cache" for d in ledger.decisions(project_id="PRJ-OPSAI")):
        did = ledger.propose({"project_id": "PRJ-OPSAI", "topic": "cache", "statement": "캐시 적용 범위를 런북 문서로 제한",
                              "rationale": "모니터링 수치는 계속 바뀌어 캐시하면 옛 값이 나옴",
                              "alternatives_rejected": ["자주 묻는 질문 Redis 캐시"],
                              "decided_by": "서지후",
                              "evidence": [{"meeting_id": "OPS-M04", "seq": q, "quote": m04[q]["text"]}
                                           for q in (10, 11)]})
        acc.record_proposal(did, "pdy", "OPS-M04")
        # 회의 기록(process_meetings.py가 채운 OPS-M04 결정 후보)에 이 원장 번호를 잇는다 — 사안 흐름·미결 연결이 원장을 가리키게
        from ledger.records import MeetingRecords  # pylint: disable=import-outside-toplevel
        rec = MeetingRecords()
        if rec.item("OPS-M04:decision:11"):
            rec.link_decision("OPS-M04:decision:11", did, "서지후")
    # 열람 요청 1건 (인턴이 참석하지 않은 회의)
    acc.request_access("yhr", "OPS-M04", "온보딩 자료 정리")
    print(f"시드 완료: 대화방 {len(THREADS)}개, 승인 대기 1건, 열람 요청 1건")


if __name__ == "__main__":
    main()
