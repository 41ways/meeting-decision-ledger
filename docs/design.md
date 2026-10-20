# 회의 결정 원장 Agent — 설계안 v0.2 (2026-10-20, 구현 반영)

과제: AI 서비스 Mini-Project, 과제 2(신규 프로젝트). 베이스 AI 에이전트(all_total_vue)의 뼈대를 재사용한다.

## 0. 한 줄 정의

회의 전사문에서 **결정과 할 일을 근거 발언과 함께 뽑고**, 그 결정이 **프로젝트 기준·과거 결정·사내 정책과 충돌하는지** 확인한 뒤, **승인된 결정만 원장(ledger)에 남긴다.** 나중에 "그건 왜 그렇게 정했지?"에 원장 → 원문 발언까지 거슬러 올라가 답한다.

기능 세 가지:
1. **결정·할 일 추출** (근거 인용 필수)
2. **충돌 감지**: 회의에서 나온 결정, 그리고 나중에 들어온 추가 의견 모두 대상
3. **근거 질의**

## 1. 과제 요구 대응

| 평가 항목 | 배점 | 이 프로젝트에서 |
|---|---|---|
| 기능 구현 완성도 | 30 | 세 기능이 화면(SSE)과 API로 동작 |
| 기술 요소 | 30 | Multi-Agent(Supervisor 흐름) · MCP(Tool/Resource/Prompt) · **RAG 4종 분리** · 의존성 주입 · LangSmith(노드별 trace) · 모듈화 · 단위 테스트 |
| 한계·트레이드오프 | 20 | §8 |
| 발표 | 20 | 문제 → 설계(RAG 분리·우선순위) → 전후 수치 → 한계 |
| Before/After | +10 | §7 LLM 단독 / 단순 RAG / 결정 원장 3단 비교 |

## 2. RAG 4종 — 담는 사실의 종류로 나눈다

| | 이름 | 담는 것 | 성격 | 수정 | 권위 |
|---|---|---|---|---|---|
| **R1** | 회의 원문 | 누가 언제 무슨 말을 했나 | **기록(증거)** | 회의마다 생성, **내용은 고치지 않음**. 보존 기한 지나면 삭제 가능 | 없음. 인용 근거로만 쓰임 |
| **R2** | 프로젝트 기준 문서 | 지금 프로젝트가 무엇을 하기로 되어 있나 (요구사항, 아키텍처) | **현재 상태(스냅샷)** | 버전 관리. 승인된 결정이 기준을 바꾸면 새 버전으로 갱신 | 결정이 없는 주제의 기본값 |
| **R3** | 결정 원장 | 언제, 누가, 무엇을, 왜, 무엇 대신 정했나 | **변경 이력(append-only)** | 추가만. 뒤집히면 새 결정이 이전 결정을 `superseded`로 만듦 | 결정된 주제에서 R2보다 우선 |
| **R4** | 거버넌스 | 사내 정책, 아키텍처 원칙, 결정 권한표 | **상위 규칙** | 최고 권한으로만 개정, 버전 관리. 일시 예외는 R4를 고치지 않고 **예외(waiver) 기록**으로 따로 | 최상위 제약 |

### 2.1 R2와 R3가 다른 점 (가장 헷갈리는 부분)

| 질문 | R2 기준 문서가 답함 | R3 결정 원장이 답함 |
|---|---|---|
| 지금 실시간 전송 방식은? | "SSE" (현재 문서 내용) | |
| 왜 SSE인가? 다른 건 검토했나? | 모름 | "10/16 PL 결정. WebSocket·Long Polling 검토 후 기각. 이유: 단방향으로 충분" |
| 예전엔 뭐였나? 언제 바뀌었나? | 모름 (새 버전이 덮어씀) | "v1은 Long Polling, 10/16 결정 OPS-0007로 대체" |
| 다른 프로젝트에선 어땠나? | 모름 | "P-2025-03 프로젝트도 SSE 채택(참고)" |

- **R2는 결과만, R3는 이유와 이력까지.** 과거 프로젝트의 방향성을 R3에서 받아 오면 "무엇을 왜 버렸는지"까지 보이니까, **예전에 기각한 대안을 다시 들고 오는 것**을 잡아낼 수 있습니다. 옛 R2 버전과 비교하면 내용 차이만 보이고 그게 의도된 변경인지 알 수 없어서, 바로 그 "옛 결과가 발목 잡는" 오탐이 생깁니다.
- 그래서 **옛 R2 버전은 감사용으로 보관만 하고 검색 대상에서 뺍니다.** 판단에는 현재 R2 + R3 이력만 씁니다.

### 2.2 R1은 왜 과거 그대로 두나

R1은 "그날 그 말을 했다"는 **사실 기록**이라 나중 결정에 맞춰 고치면 증거가 훼손됩니다. 대신 R1은 **"지금 유효한 게 뭐냐"를 판단하는 데 아예 쓰지 않습니다.** 인용과 "아직 결정 안 된 논의" 찾기에만 씁니다. 그래서 R1이 옛날 내용을 담고 있어도 충돌을 일으키지 않습니다.

## 3. 우선순위와 해석 규칙 (옛 결과가 발목 잡지 않게)

### 3.1 지금 유효한 기준을 정하는 순서

```
R4 (현재 버전 정책)         ← 제약. 아래 어떤 결정도 R4를 어기면 무효 (유효한 예외가 있으면 예외 표시와 함께 허용)
  ↓
R3 (status=approved, superseded 아닌 결정)   ← 결정된 주제는 여기가 정답
  ↓
R2 (현재 버전 문서)          ← 결정이 없는 주제의 기본값
  ↓
R1                           ← 권위 없음. "논의는 있었지만 미결정"을 알려줄 때만
```

### 3.2 같은 주제가 여러 곳에 있으면

모든 레코드에 `topic`(주제 키, 예: `realtime.transport`)을 붙여서 주제 단위로 맞춥니다.

| 상황 | 처리 |
|---|---|
| R3에 유효한 결정이 있음 | R3를 채택. 같은 주제의 R2 내용이 다르면 "R2 갱신 필요"로 표시만 함 (충돌로 세지 않음) |
| R3에 여러 결정 | `effective_from`이 가장 최근이고 `superseded_by`가 비어 있는 것 하나 |
| R3에 없고 R2에 있음 | R2(현재 버전) 채택 |
| R3·R2 모두 없음 | R1에서 관련 발언 검색 → "10/16 논의는 있었지만(화자, 시각) 결정되지 않음" |
| R3에 `proposed`(승인 대기) | 권위 없음. "승인 대기 중"으로만 안내 |
| 과거 다른 프로젝트 결정 | `project_id`가 달라서 **참고(advisory)**로만. 같은 프로젝트 결정보다 항상 아래 |

### 3.3 결정이 승인되면 일어나는 일 (갱신 메커니즘)

```
회의 → 추출된 결정 (proposed) ─사람 승인→ R3에 approved로 추가
                                         ├ 같은 topic의 이전 결정 → superseded_by = 새 결정
                                         └ 이 결정이 R2 기준을 바꾸면 → R2 새 버전 생성 요청 (diff 기록)
```

- 검색은 항상 `status=approved AND superseded_by IS NULL`, R2는 `is_current=true`만 대상으로 합니다. 그래서 수정 전 결과가 검색에 끼어들지 않습니다.
- R2 갱신이 늦어져도 §3.2 규칙에서 R3가 이기므로 판단은 틀리지 않습니다.

### 3.4 R4 정책과 예외(백도어)

- R4는 `policy_version`, `effective_from`으로 버전 관리합니다. 개정은 최고 권한 경로로만 하고, 회의 파이프라인에서는 R4를 절대 쓰지 않습니다(읽기 전용).
- 사람이 따로 허용하는 **일시 예외는 `waivers` 테이블**에 둡니다. 항목은 대상 규칙, 범위, 승인자, **만료일**입니다.
- 게이트 판단:

```
결정이 R4 규칙 위반?
  ├ 아니오 → 통과
  └ 예 → 유효한(만료 전, 범위 일치) waiver 있음?
          ├ 예 → 통과 + "예외 승인 W-003(만료 11/30)" 표시
          └ 아니오 → 차단 + "승인 필요: 보안 담당(규칙 SEC-04, 정책 v3)"
```

- 예외는 R4를 바꾸지 않습니다. 만료되면 자동으로 다시 차단됩니다. 같은 규칙에 예외가 반복되면 "정책 개정 검토 필요" 신호로 집계합니다.
- 결정권자 검사도 여기서 1차로 합니다. 결정 권한표(R4)에서 분야별 승인자를 찾아, 회의에서 결정한 사람이 그 권한을 가졌는지 봅니다.

## 4. 데이터 모델

```text
R1 segment   {meeting_id, seq, speaker, role, ts, text, project_id, domain}
R2 chunk     {doc_id, doc_version, is_current, section, topic, text, project_id, domain}
R3 decision  {decision_id, project_id, topic, statement, rationale, alternatives_rejected[],
              decided_by, approved_by, status(proposed|approved|rejected),
              effective_from, superseded_by, evidence[{meeting_id, seq, quote}]}
R4 rule      {rule_id, policy_version, is_current, domain(authority|security|architecture|operation|fairness),
              topic, text, approver_role, threshold}
waiver       {waiver_id, rule_id, project_id, scope, approved_by, expires_at, reason}
```

- 진실의 원천은 SQLite(`store/ledger.db`). Chroma는 찾기용 색인(컬렉션 4개, cosine)
- 임베딩: 로컬 `qwen3-embedding:4b` (질의에 Instruct 접두어). 회의록 기밀성 때문에 외부 API 없음
- 주제 키 목록은 `data/project/topics.json` → MCP Resource `ledger://topics`

## 5. 파이프라인 (베이스 노드 그대로)

```
route ─┬ chat ─────────────────────────────────────────────── chat_answer
       └ meeting / interview / opinion / question
            → plan → gate → research ⇄ replan_check → analysis → write ⇄ validate → finalize
```

| 노드 | 베이스 | 이 프로젝트 | LLM / 코드 |
|---|---|---|---|
| route | chat / 파이프라인 | 회의·면접·의견·질의·잡담. 회의 ID와 동사로 먼저 분류, 애매하면 fast 모델 | 규칙 → LLM |
| plan | MCP Prompt + 태스크 분해 | MCP Prompt(추출 절차) + Resource(주제 목록·용어집). 규칙으로 결정 후보 표시 → LLM이 분류·요약·주제 선택 | 규칙 + LLM |
| gate | analysis 태스크 강제 제거 | 확정 표현도 진행자 발언도 아니면 결정에서 제외. 인용·담당자·기한을 원문 대조로 확정 | 코드 |
| research | 병렬 조회 + 재시도 | 결정마다 `resolve_current`(R4>R3>R2), `check_authority`, `check_waiver`. 동시 2개, 실패 격리 | 코드(MCP) |
| replan_check | 런북 없으면 재조사 | 유효한 결정·문서가 없으면 R1 원문 논의 추가 조회 (1회) | 코드 |
| analysis | 비교 분석 | 기준마다 same/different를 LLM이 답하고 판정표가 충돌 종류 결정 | LLM + 코드 |
| write | 고품질 모델 작성 | 회의·면접·의견은 템플릿(ID·숫자 고정), 질의만 LLM 토큰 스트리밍 | 코드 / LLM |
| validate | 길이·번호 검사 → 재작성 | 인용 글자 일치, 질의 답변에 결정 ID·"승인 대기"·"결정되지 않음" 표기. 최대 3회 | 코드 |
| finalize | 최종 답변 | 문제없는 결정을 승인 대기로 원장 등록 (서버 실행 시) | 코드(MCP) |

SSE 이벤트: RouteDecided, PlanCreated, GateApplied, DecisionFound, FairnessFlag, TaskCompleted, ReplanCheck, ConflictChecked, DraftWritten, Validation, AnswerToken, Finalize.

충돌 판정표 (`decide()`):

| LLM 답 | 기준 종류 | 결과 |
|---|---|---|
| different | 정책 | 정책 충돌. 범위가 맞는 유효 예외가 있으면 "예외 적용" |
| (무관) | 권한표 | `check_authority` 실패면 권한 충돌 (코드가 추가) |
| different | 유효 결정·기준 문서 | 권한자 승인(또는 예외)이고 다른 충돌이 없으면 "기준 변경", 아니면 충돌 |
| different | 다른 프로젝트 결정 | 참고 (충돌 아님) |
| same | 승인 대기 결정 | 그 결정을 확정하는 발언 |

## 6. MCP 서버 (`mcp_ledger_server.py`, FastMCP stdio)

| 종류 | 이름 | 이유 |
|---|---|---|
| Tool | `get_segments`, `search_transcript` | 회의마다 다른 원문 (R1) |
| Tool | `search_decisions`, `get_decision`, `list_decisions` | 계속 쌓이는 원장 (R3) |
| Tool | `search_baseline`, `search_policies` | 현행 버전만 검색 (R2·R4) |
| Tool | `resolve_current`, `check_authority`, `check_waiver`, `topic_candidates` | 우선순위 해석과 결정적 검사 |
| Tool | `propose_decision` / `approve_decision`(사람 전용) | 원장 쓰기 |
| Resource | `policy://{version}/{rule_id}`, `project-doc://{doc_id}/current`, `ledger://topics` | 거의 안 바뀌는 문서 |
| Prompt | `extract_decisions_prompt`, `conflict_review_prompt`, `summarize_interview_prompt` | 표준 절차 |

## 7. 평가 (Before/After)

`eval/run_eval.py` — 같은 입력(회의 OPS-M03~OPS-M05, 추가 의견 5건, 근거 질의 6건)을 세 방식으로 실행. 결과는 `eval/results.md`.

- Before-A LLM 단독: 회의록·질문만 주고 한 번에 답
- Before-B 단순 RAG: 4종 자료를 버전·대체 구분 없이 검색해 붙임
- After: 이 그래프

지표: 결정 재현율·정밀도, 회상 발언 오추출, 인용 원문 일치, 할 일 담당·기한, 충돌 탐지율·오탐, 의견 판정, 대체된 결정 인용(옛 결과 간섭), 근거 질의 정답, 처리 시간·LLM 호출·토큰.

## 8. 한계와 트레이드오프

[ADR 문서](adr.md)에 선택별로 정리. 요약:

- 평가 데이터(회의 5건, 면접 2건)와 정답셋을 직접 만들었고, 틀린 사례를 보고 고친 뒤 같은 데이터로 다시 쟀다. 수치는 일반화 성능이 아니다
- 결정 후보 규칙(확정 표현·진행자 발언)은 한국어 회의 말투에 의존
- 주제 키 선택이 틀리면 엉뚱한 기준과 비교 (주제 목록 수동 관리)
- 권한 검사는 "결정자 또는 참석자 중 권한자"로 본다. 권한자가 참석만 하고 동의하지 않은 경우를 구분하지 못한다
- 처리 시간이 LLM 단독보다 3배 길다 (호출 수 증가). 로컬 8B 모델 기준 회의 1건 40~55초
- 사람 승인이 병목이 될 수 있고, 승인 전 결정은 권위가 없다
- 화자 분리는 입력에 있다고 가정 (Whisper 단독으로는 화자 구분이 안 됨)

## 9. 일정

| 언제 | 한 일 |
|---|---|
| D1 오전 | 베이스 실행·분석, 주제 선정, 설계 |
| D1 오후 | 데이터·정답셋, RAG 4종 적재, MCP 서버, 그래프, 서버·화면, 평가, 면접 전사문 정리 |
| D2 | 실측 보완, 발표 자료, 시연 리허설 |
| D3 ~13:00 | README 최종, 제출 |

## 10. 면접 전사문 정리

전사문 정리의 한 종류로 면접 전사문을 받는다. 같은 그래프에서 plan·write만 도메인별로 나뉜다.

- R1: 면접 전사문 (면접관·지원자 발언), R4: 공정성 규칙 FAIR-01(직무 무관 개인정보 질문 금지, 채용절차법 제4조의3 취지)·FAIR-03(면접 중 개인의 합격 확정 금지)
- 출력: 질문 요약·답변 요약 쌍(발언 번호), 공정성 점검. 점수·합격 여부는 내지 않는다
- 공정성 점검 후보는 면접관 발언에서 규칙으로 찾고, 해당 조항은 MCP Resource로 직접 가져와 판정한다
- 면접 전체를 다루는 별도 에이전트는 이 프로젝트와 다른 방향의 후속 과제로 둔다
