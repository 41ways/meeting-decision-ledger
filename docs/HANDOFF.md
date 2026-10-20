# 인계 — meeting-decision-ledger (회의 요약 · 결정 정리 비서)

**2026-10-20 (화) 저녁 · 맥북 → 집 데스크톱**

---

## 0. 30초 요약

SKALA AI Capstone 과제 2. 회의 전사문에서 결정·할 일을 근거와 함께 뽑고, 사내 정책·과거 결정과 충돌하면 해결 방향까지 내며, 팀장이 승인한 결정만 원장에 남기는 에이전트.
오늘 설계 재구성안대로 회의 기록 층·해결 제안·사실 질문·후보 선택 검색·승인함/미결함을 구현했고 전부 푸시했다.
테스트 111개 통과, 전체 평가 판정 지표 100% 유지(처리 23.5초·LLM 호출 63회). 마지막 커밋 `067bdc9`.

---

## 1. 저장소와 이 맥북에만 있는 것

| 항목 | 값 |
| --- | --- |
| 저장소 | `41ways/meeting-decision-ledger` (public) · 브랜치 `main` |
| 맥북 로컬 | `~/workspace/skala-capstnproj/meeting-decision-ledger` |
| 맥북에만 있음 | `.env`(LangSmith 키 — 키는 직접 다시 넣는다), `store/`(원장·색인 — 스크립트로 재생성), `session.db` |
| 발표 자료 | https://claude.ai/artifact/QRWEgspT5Meao14ndZDdTM (9장, 비공개) · 대본 `docs/발표대본.md` |

---

## 2. 집에서 이어받기

```bash
git clone https://github.com/41ways/meeting-decision-ledger.git   # 이미 있으면 git pull
cd meeting-decision-ledger
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ollama pull qwen3:8b && ollama pull qwen3:0.6b && ollama pull qwen3-embedding:4b
cp .env.example .env        # LangSmith 쓰려면 LANGSMITH_API_KEY 등을 직접 채운다
bash scripts/reset_demo.sh  # 원장·색인·회의 기록·시연 대화방 생성 (회의 5개 실제 정리, 6~7분). 빨리: SKIP_PROCESS=1 bash scripts/reset_demo.sh
bash sh/run.sh              # http://127.0.0.1:8100
.venv/bin/python -m pytest -q
```

---

## 3. 오늘 한 일 (커밋 순)

1. 검색 하한·주제 두 경로 대조 (`40bdc99`) — 조용한 폴백·없는 논의 차단, 판정 실패/기준 없음 상태 분리, 답변 값 대조. 이슈 #2로 기록
2. 일정 추가 PR #1 병합 (`fdd583b`)
3. 회의 기록 층 `ledger/records.py` (`20ce3b8`) — meetings·items·threads·events
4. 회의 정리 보강 `agent/enrich.py` (`39a9073`) — 상태 라벨, 해결 제안, 칸 기록, 근거 달린 요약, 미결 연결, 인사이트
5. 질문 경로 `agent/facts.py`·`agent/retrieve.py` (`ed60290`) — 사실 질문(표 조회), '이거' 풀이, 후보 넓게 + LLM은 ID만, 라벨, 사안의 흐름
6. 승인함·미결함·보존 스크립트 (`d2aa71d`), 의견 경로 회귀 수정 (`1163347`)
7. 재평가 반영·시연 목업을 실제 출력으로 교체·README 화면 갱신 (`6c6cec5` ~ `067bdc9`)

설계 근거: `docs/설계_재구성안.md`(8절에 구현 현황), 오점 목록: `docs/오점_점검.md`

---

## 4. 남은 일 (순서대로)

1. **발표 자료 렌더링 확인** — 해법·Before/After·한계·시연 장표를 수정했지만 화면으로 다시 안 봄
2. **시연 리허설** — `reset_demo.sh` 후 대본 순서대로: 서지후 M04 정리 → 정한결 승인함(원문 인용·수정) → 미결함 → 캐시 질문 → "이거 누가 승인했어?"
3. **미결함의 OPS-M01 '외부 LLM API 사용 여부'** — 같은 회의에서 정해진 것을 미결로 뽑은 추출 오류. 시연에서 '닫기' 예시로 쓰거나 원인 확인
4. 새 모듈(enrich·facts·retrieve·records) pylint 재확인 (전체는 9.84였음)
5. 구글 시트에 주제·Git URL 등록 (직접)
6. 미구현(발표에서 한계로): 서버 측 로그인(SSO), 색인 일일 점검·드리프트 지표, 비동기 정리 워커, 예상 질문 색인

---

## 5. 주의

- 공개 저장소 — 강의 PDF·원본 자료, API 키를 올리지 않는다
- `store/`는 생성물이라 커밋하지 않는다. 꼬이면 `reset_demo.sh`로 다시 만든다
- 평가는 임시 사본으로 돈다: `.venv/bin/python eval/run_eval.py --only after` (10분 안팎, 로컬 LLM)
- 시연 대화방은 실제 출력을 옮긴 목업. 새로 입력하는 질문만 실시간 실행
