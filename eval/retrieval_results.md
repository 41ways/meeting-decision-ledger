# 검색 품질 평가

점수 = cosine 유사도. 분포는 최소 / 중앙 / 최대.

## 요약

| 항목 | 값 |
|---|---|
| 주제 top1 정확도 | 86% |
| 주제 top3 포함률 | 86% |
| 주제 1위 점수 — 정답일 때 | 0.553 / 0.716 / 0.812 |
| 주제 1위 점수 — 함정(새 주제) | 0.376 / 0.406 / 0.463 |
| 주제 1·2위 점수 차 (정답 있는 문장) | 0.001 / 0.222 / 0.372 |
| 원문 hit@3 | 100% |
| 원문 점수 — 관련 발언 | 0.626 / 0.712 / 0.859 |
| 원문 점수 — 상위 3위 안 무관 발언 | 0.546 / 0.670 / 0.751 |
| 원문 1위 점수 — 함정(논의 없음) | 0.390 / 0.489 / 0.527 |

## 하한 적용 후 (주제 0.5, 원문 0.58)

| 항목 | 값 |
|---|---|
| 새 주제(함정)에 기존 주제를 붙인 수 | 0 / 8 |
| 맞힌 주제를 하한 때문에 버린 수 | 0 / 19 |
| 논의 없는 질문에 '관련 논의'를 붙인 수 | 0 / 8 |
| 관련 발언을 하한 때문에 버린 수 | 0 / 20 |

## 주제 후보

| | 문장 | 기대 | 후보 (점수) | 1·2위 차 |
|---|---|---|---|---|
| ✓ | 실시간 전송을 SSE로 한 이유가 뭐였죠? | realtime.transport | realtime.transport 0.783, interview.competency 0.429, tech.backend 0.429 | 0.354 |
| ✓ | 대화 저장소는 왜 PostgreSQL이에요? | session.store | session.store 0.802, routing 0.444, llm.external 0.422 | 0.359 |
| ✗ | Redis 캐시 쓰기로 했어요? | cache | session.store 0.632, interview.question 0.430, answer.validation 0.413 | 0.202 |
| ✓ | 모델 티어링은 정해졌나요? | llm.tiering | llm.tiering 0.666, llm.external 0.521, agent.structure 0.447 | 0.146 |
| ✓ | 예전 고객센터 챗봇에서는 왜 웹소켓을 그만뒀어요? | realtime.transport | realtime.transport 0.631, session.store 0.463, routing 0.444 | 0.168 |
| ✓ | 응답 시간 기준이 지금 몇 초예요? | perf.latency | perf.latency 0.756, realtime.transport 0.479, llm.external 0.476 | 0.277 |
| ✓ | 운영 로그 원문을 외부 Claude API로 보내서 요약하면  | llm.external | llm.external 0.720, agent.structure 0.478, tech.backend 0.476 | 0.242 |
| ✓ | 대화 저장을 Redis로 바꾸면 더 빠르지 않을까요? | session.store | session.store 0.812, realtime.transport 0.485, routing 0.473 | 0.327 |
| ✓ | 마스킹한 로그 샘플 100건으로 외부 모델 품질 비교를 해 보 | llm.external | llm.external 0.596, llm.tiering 0.469, perf.latency 0.379 | 0.127 |
| ✓ | 실시간 전송을 다시 롱폴링으로 돌리면 어떨까요? | realtime.transport | realtime.transport 0.716, tech.backend 0.414, perf.latency 0.405 | 0.302 |
| ✓ | 실시간 전송을 WebSocket으로 변경 | realtime.transport | realtime.transport 0.746, perf.latency 0.374, tech.backend 0.373 | 0.372 |
| ✓ | 운영 반영을 2주 연기 | schedule | schedule 0.609, perf.latency 0.508, scope.core 0.391 | 0.101 |
| ✓ | 실제 운영 로그를 외부 LLM API에 넣어 품질 비교 | llm.external | llm.external 0.695, tech.backend 0.450, llm.tiering 0.422 | 0.244 |
| ✗ | 캐시는 런북 문서에만 적용하고 모니터링 조회는 매번 새로 | cache | scope.core 0.490, mcp.deployment 0.473, realtime.transport 0.444 | 0.017 |
| ✓ | 최종 답변 기준을 60초로 변경 | perf.latency | perf.latency 0.718, routing 0.451, answer.grounding 0.440 | 0.267 |
| ✓ | 대화 저장 DB를 MySQL로 변경 | session.store | session.store 0.716, routing 0.405, llm.external 0.371 | 0.311 |
| ✗ | 회의 원문은 90일 후 파기하고 결정 근거 인용만 원장에 보존 | meeting.retention | llm.external 0.469, answer.grounding 0.432, interview.competency 0.417 | 0.037 |
| ✓ | 분류·계획은 작은 모델, 작성·검증은 큰 모델로 확정 | llm.tiering | llm.tiering 0.653, agent.structure 0.525, answer.validation 0.492 | 0.128 |
| ✓ | MCP 서버를 별도 컨테이너로 띄우는 건 어때요? | mcp.deployment | mcp.deployment 0.739, tech.backend 0.462, agent.structure 0.402 | 0.277 |
| ✓ | 질문을 쉬운 것과 어려운 것으로 먼저 나눠서 처리하자 | routing | routing 0.613, agent.structure 0.468, perf.latency 0.447 | 0.144 |
| ✓ | 백엔드를 Spring으로 바꾸면 어떨까요? | tech.backend | tech.backend 0.553, interview.question 0.552, interview.competency 0.503 | 0.001 |
| ✓ | Supervisor 대신 단일 Agent로 단순화하자 | agent.structure | agent.structure 0.707, tech.backend 0.508, llm.tiering 0.408 | 0.199 |
| 함정 | 사내 메신저를 슬랙에서 팀즈로 바꾸면 어때요? | (없음) | interview.competency 0.460, realtime.transport 0.451, llm.external 0.438 | 0.009 |
| 함정 | 다음 주 회식 장소를 정하자 | (없음) | interview.competency 0.376, llm.external 0.364, perf.latency 0.364 | 0.011 |
| 함정 | 프론트엔드 디자인 시스템을 머티리얼로 통일하자 | (없음) | interview.question 0.381, tech.backend 0.376, mcp.deployment 0.346 | 0.005 |
| 함정 | 사무실 좌석 배치를 팀별로 바꾸자 | (없음) | interview.competency 0.407, agent.structure 0.401, interview.question 0.330 | 0.007 |
| 함정 | 코드 리뷰는 두 명이 승인해야 머지하는 걸로 하죠 | (없음) | interview.competency 0.463, answer.validation 0.377, interview.question 0.367 | 0.086 |
| 함정 | 깃 브랜치 전략을 trunk 기반으로 바꾸자 | (없음) | interview.competency 0.427, realtime.transport 0.368, interview.question 0.334 | 0.059 |
| 함정 | 회의실 예약 시스템을 새로 도입하자 | (없음) | schedule 0.404, llm.external 0.395, interview.competency 0.385 | 0.009 |
| 함정 | 쿠버네티스 오토스케일링 기준을 CPU 70%로 하자 | (없음) | llm.external 0.388, agent.structure 0.387, llm.tiering 0.375 | 0.001 |

## 회의 원문

| | 질의 | 상위 3 (점수) |
|---|---|---|
| ✓ | 실시간 전송을 SSE로 한 이유 | OPS-M02:8 0.771, OPS-M02:7 0.736, OPS-M02:9 0.725 |
| ✓ | 대화 저장소는 왜 PostgreSQL이에요? | OPS-M02:11 0.699, OPS-M02:10 0.696, OPS-M05:5 0.670 |
| ✓ | Redis 캐시 쓰기로 했어요? | OPS-M03:9 0.710, OPS-M03:8 0.705, OPS-M04:10 0.626 |
| ✓ | 모델 티어링은 정해졌나요? | OPS-M02:19 0.727, OPS-M05:9 0.712, OPS-M02:15 0.711 |
| ✓ | 응답 시간 기준이 몇 초예요? | OPS-M02:3 0.724, OPS-M01:11 0.697, OPS-M02:1 0.663 |
| ✓ | 검증 노드에서 재작성은 몇 번까지 하기로 했어요? | OPS-M03:3 0.690, OPS-M03:2 0.655, OPS-M03:4 0.587 |
| ✓ | MCP 타임아웃 재시도는 어떻게 하기로 했죠? | OPS-M03:6 0.765, OPS-M03:7 0.697, OPS-M03:5 0.670 |
| ✓ | 회의 원문 보관 기간은? | OPS-M05:7 0.859, OPS-M05:8 0.803, OPS-M05:6 0.637 |
| ✓ | 로그에 섞인 고객 전화번호 마스킹 | OPS-M05:11 0.812, OPS-M05:12 0.756, OPS-M05:10 0.546 |
| ✓ | 외부 LLM으로 품질 비교하는 건 허용됐어요? | OPS-M02:16 0.775, OPS-M02:15 0.751, OPS-M04:8 0.734 |
| 함정 | 사내 메신저를 슬랙에서 팀즈로 바꾸기로 했나요? | OPS-M05:5 0.527, OPS-M05:15 0.521, OPS-M04:6 0.498 |
| 함정 | 회식 장소는 어디로 정했어요? | OPS-M05:5 0.420, OPS-M05:4 0.411, OPS-M03:13 0.407 |
| 함정 | 프론트엔드 디자인 시스템은 뭘 쓰기로 했어요? | OPS-M01:2 0.512, OPS-M01:12 0.492, OPS-M01:4 0.488 |
| 함정 | 코드 리뷰 승인 인원은 몇 명이에요? | OPS-M04:8 0.496, OPS-M05:13 0.484, OPS-M02:3 0.475 |
| 함정 | 깃 브랜치 전략은 정했나요? | OPS-M01:1 0.465, OPS-M03:5 0.462, OPS-M01:2 0.461 |
| 함정 | 회의실 예약 시스템 도입 얘기 있었어요? | OPS-M04:1 0.501, OPS-M05:5 0.469, OPS-M05:4 0.469 |
| 함정 | 쿠버네티스 오토스케일링 기준은요? | OPS-M03:9 0.390, OPS-M01:8 0.384, OPS-M04:9 0.381 |
| 함정 | 신입 온보딩 교육은 언제 하기로 했어요? | OPS-M02:18 0.481, OPS-M01:14 0.463, OPS-M02:17 0.460 |
