# 베이스 AI 에이전트 실행 분석 (2026-10-20)

환경: 맥북 로컬, `LLM_BACKEND=ollama` (fast=qwen3:0.6b, quality=qwen3:8b), LangSmith 기록 끔.
테스트: `doc/regression_test_queries.md` 10개 + 규칙에 없는 표현 2개(x1, x2). 원본 응답은 `results.json`.

## 1. 베이스가 하는 일

```
route ─ chat → chat_answer (바로 답)
      └ incident → plan → gate → research → replan_check ─(런북 없음)→ research
                                                      └(있음)→ analysis → write → validate ─(불합격)→ write
                                                                                          └(합격)→ finalize
```

| 노드 | 하는 일 | 쓰는 것 |
|---|---|---|
| route | chat / incident 분류 | fast_llm (0.6b) |
| plan | MCP Prompt로 진단 절차를 받아 조사 대상 시스템별 task 생성 | fast_llm, MCP Prompt |
| gate | needs_analysis=False인데 analysis task가 있으면 코드가 제거 | 코드 |
| research | 대상 시스템 모니터링을 병렬 조회 | MCP Tool, with_retry |
| replan_check | 런북이 없으면 장애 유형을 **키워드 규칙**으로 정해 런북 조회 task 추가 | 코드 (`_INCIDENT_TYPE_RULES`) |
| analysis | analysis task가 있으면 여러 시스템 비교 | fast_llm (0.6b) |
| write | 수집 사실로 1) 2) 3) 보고 작성 | quality_llm (8b) |
| validate | 30자 이상 + "1)" 포함이면 합격, 최대 3회 | 코드 |

## 2. 결과 요약

| # | 질문 | 시간 | 조사한 시스템 | 런북 | 판정 |
|---|---|---|---|---|---|
| q1 | payments CPU 급증 | 66.8s | payments | cpu-spike | 맞음 |
| q2 | cache 메모리 증가 | 34.6s | cache | memory-leak | 맞음 |
| q3 | log 디스크 부족 | 64.6s | log | disk-full | 맞음 |
| q4 | gateway → payments 타임아웃 | 47.3s | gateway만 | network-timeout | **payments 누락** |
| q5 | web-01 CPU + auth 커넥션 풀 | 34.1s | auth, web-01 | high-latency만 | web-01 CPU 대응 없음 |
| q6 | payments·cache 공통 원인 | 62.7s | 둘 다 | cpu-spike만 | **cache용 memory-leak 런북 누락**, 분석 결과 "특이 패턴 없음" 9자 |
| q7 | 로그인 느림 (시스템 미언급) | 34.2s | auth (기본값) | high-latency | 우연히 맞음 |
| q8 | billing-service 느림 (없는 시스템) | 35.5s | **auth** | high-latency | **billing을 auth 데이터로 진단, 모니터링 대상이 아니라는 말 없음** |
| q9 | 안녕, 너는 누구야? | 1.4s | - | - | "저는 빠르게 비서입니다" (어색) |
| q10 | 이름 기억 | 1.0s | - | - | 기억은 하지만 "저의 이름은 민수입니다"라고 답함 |
| x1 | 결제가 자꾸 중간에 끊겨요 | 21.9s | **auth** | high-latency | **틀림** (payments·gateway여야 함) |
| x2 | 서버 디스크가 꽉 찼대요 | 23.9s | **auth** | **high-latency** | **완전히 틀림** (log-service disk-full이 실제로 있음) |

모든 incident 질문이 검증 1회 만에 합격. 틀린 진단(q8, x1, x2)도 그대로 통과.

## 3. 부족한 점

### A. 질문을 이해하는 부분이 가장 약함 (심각)
1. **시스템을 이름 문자열로만 찾음.** `_extract_system_name`, `_guess_target`이 질문에 `payments-service` 같은 정확한 이름이 있어야 인식. "결제", "디스크", "로그인" 같은 일상 표현은 못 알아보고 **auth-service로 조용히 폴백**.
2. **장애 유형을 사용자 말이 아니라 (잘못 고른) 시스템의 알림 키워드로 정함.** `_classify_incident_type`이 모니터링 알림 문구에서 키워드를 찾기 때문에, 엉뚱한 시스템을 고르면 런북도 연쇄적으로 틀림(x2: 디스크 질문에 지연 런북). 키워드 순서도 고정이라 latency/pool이 먼저 걸리면 다른 증상은 무시.
3. **모르는 시스템이라고 말하지 않음.** billing-service처럼 모니터링에 없는 시스템을 auth-service 데이터로 진단하고, 답에서 그 사실을 밝히지 않음. 운영 환경에서는 가장 위험한 실패(그럴듯한 오답).

### B. 다중 시스템 처리
4. 두 시스템 질문에서 한쪽 누락(q4). 작은 계획 모델이 대상을 하나만 뽑음.
5. **런북을 질문당 하나만 가져옴.** 시스템마다 증상이 달라도(q5, q6) 첫 키워드에 걸린 유형 하나만 조회.

### C. 품질 장치가 형식만 봄
6. **검증이 "30자 + 1)"뿐.** 질문한 시스템을 다뤘는지, 수치가 실제 수집값인지, 런북과 맞는지 안 봄. 그래서 틀린 진단도 전부 1회 합격.
7. **재계획이 고정 단계.** "런북이 있나"만 확인해서 1차 조사 후 항상 1회 재계획. 판단이 아니라 순서.
8. **분석을 0.6b 모델이 함.** 공통 원인 질문(q6)에도 "특이 패턴 없음" 9자. 비교 분석이 사실상 비어 있음.

### D. 그 밖
9. 응답 시간 22~67초 (로컬 8b 작성 단계가 대부분, research의 0.35초 고정 지연은 시연용).
10. chat 경로 답이 어색함(0.6b): "빠르게 비서", 이름을 자기 이름처럼 말함.
11. 학생용 zip에 평가 스크립트(`eval/`)가 없음. 정답이 붙은 질의셋도 없음 → Before/After 비교를 하려면 직접 만들어야 함.
12. 하드코딩: 기본 시스템 auth-service, 폴백 런북 db-down, 시스템 목록이 monitoring.json 키에 고정.

## 4. 수정할 사항 후보 (과제 1 개선 기준)

| 우선 | 무엇을 | 어떻게 | Before/After 지표 | 기술 요소 |
|---|---|---|---|---|
| 1 | 질문 → 시스템·장애 유형 판별 | 런북·시스템 설명을 벡터 DB에 넣고 질문과 의미 검색 (RAG). 메타데이터에 incident_type, 관련 시스템 | 정답 질의셋에서 시스템 정답률, 런북 정답률 | RAG, MCP |
| 2 | 모르는 시스템·모호한 질문 처리 | 유사도가 낮으면 폴백하지 말고 "모니터링 대상이 아님 / 어느 시스템인지" 되묻기 | 오진단 건수 (q8, x1, x2 같은 유형) | 라우팅, 게이트 |
| 3 | 시스템별 런북 | 대상 시스템마다 장애 유형을 정해 런북 여러 개 조회 | q5·q6 같은 다중 질문에서 런북 커버율 | 병렬, MCP Resource |
| 4 | 검증 강화 | LLM-as-a-Judge: 질문한 시스템을 다뤘나, 수치가 수집값과 같나, 런북과 맞나 | 오답 차단율, 재작성 횟수 | Validator, LangSmith 평가 |
| 5 | 평가셋과 LangSmith | 정답 붙은 질의 30개 + Dataset·evaluate로 전후 자동 비교 | 위 지표 전부 | LangSmith, 단위 테스트 |
| 6 | 모듈화 | graph.py 노드를 파일로 나누고 판별 로직에 pytest | 테스트 수 | 모듈화, 의존성 주입 |

트레이드오프로 짚을 것: 임베딩 비용과 첫 응답 지연 증가, 벡터 DB 의존성, 런북이 바뀌면 재임베딩, 되묻기가 늘면 사용자 경험 저하, LLM 심판 호출로 비용·시간 증가.

## 5. 새 주제로 갈 경우에도 고쳐야 할 것
도메인을 바꿔도 위 A·C는 구조 문제라 그대로 따라옵니다. 이름 정확 매칭 + 기본값 폴백, 키워드 규칙, 형식만 보는 검증, 고정 재계획. 신규 주제라면 처음부터 의미 검색 기반 판별과 내용 검증을 넣는 설계가 필요합니다.
