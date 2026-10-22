# meeting-decision-ledger — 회의 요약 · 결정 정리 비서

회의 전사문에서 **결정과 할 일을 근거 발언과 함께** 뽑고, 그 결정이 과거 결정·프로젝트 기준 문서·사내 정책과 충돌하는지 확인한 뒤, **사람이 승인한 결정만 원장(ledger)에 남기는** AI Agent. 나중에 "그건 왜 그렇게 정했지?"에 원장 → 원문 발언까지 거슬러 올라가 답한다.

SKALA AI Capstone 미니 프로젝트 · 과제 2(신규 AI 서비스). 강사 배포 `all_total_vue_student`의 구조(LangGraph 노드, MCP stdio, FastAPI·SSE, Vue 화면, SQLite 세션, 의존성 주입)를 재사용했다. 결과 보고서: [docs/결과보고서.md](docs/결과보고서.md)

<a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/m04_report.jpg"><img src="docs/images/m04_report.jpg" width="100%" alt="회의 정리 화면"></a>

## 1. 해결하려는 문제

회의가 끝나면 사람이 하는 일 — 누가 뭘 정했는지 골라내기, 지난 결정·규정과 맞는지 뒤지기, 몇 주 뒤 "왜 그렇게 정했어?"에 원문 다시 읽기. 회의 내용은 기밀이라 외부 LLM에 맡기기도 어렵다.

가상 프로젝트(운영 장애 대응 AI, 회의 OPS-M01~M05)에 실제로 자주 생기는 상황을 넣었다.

| 상황 | 놓치면 생기는 일 |
|---|---|
| SSE로 정한 뒤, PM 불참 회의에서 WebSocket으로 다시 바꿈 (OPS-M04) | 과거 결정·아키텍처 원칙 위반을 아무도 모름 |
| PL 혼자 운영 반영을 2주 연기 (OPS-M04) | 2주 이상 지연은 본부장 승인 사항 |
| 구 정책(MySQL 표준)을 근거로 MySQL 결정 (OPS-M05) | 현행 정책은 PostgreSQL |
| "예전에 롱폴링에서 SSE로 바꾸고 나서…" 회상 발언 | 결정으로 오인하거나 대체된 결정과 비교해 오탐 |

LLM 단독은 사내 규정을 몰라 못 잡고, 단순 RAG는 이미 대체된 결정을 현재 기준으로 들어 오탐한다(5장 실측).

## 2. 기술 스택

| 영역 | 기술 |
|---|---|
| Agent | LangGraph StateGraph 13노드, 의존성 주입(`LedgerDeps` + `partial`) |
| MCP | FastMCP stdio 서버 — Tool 35 · Resource 3 · Prompt 3. 그래프는 원장 DB를 직접 열지 않고 도구로만 접근 |
| RAG | Chroma 4종(cosine) + SQLite(원장·정책·예외의 원천) + SQLite 낱말 색인 |
| LLM | 로컬 Ollama `qwen3:8b`(추출·판정·작성), `qwen3:0.6b`(잡담), 임베딩 `qwen3-embedding:4b`. 외부 전송 없음 |
| Backend · Frontend | Python 3.11, FastAPI, SSE · Vue 3(CDN, 빌드 없음) |
| 추적 · 테스트 | LangSmith `@traceable` · pytest 204개(가짜 LLM·MCP 주입, Ollama 없이 수 초) |

## 3. 아키텍처

```
브라우저(Vue) ──SSE──▶ FastAPI(server.py) ──▶ LangGraph(agent/ledger_graph.py)
                                                  │  MCP stdio
                                                  ▼
                                   mcp_ledger_server.py ──▶ SQLite 원장·정책·회의 기록
                                                        ├─▶ SQLite 낱말 색인 (결정·미결 별칭 / 회의 원문 BM25)
                                                        └─▶ Chroma R2·R4 검색, R3 주제 대조
```

```
route ─┬ chat / schedule / schedule_add / fact (사실 질문은 LLM 없이 표 조회)
       └ meeting / interview / opinion / question
            → plan → gate → research ⇄ replan_check → analysis → write ⇄ validate → finalize
```

**RAG 4종과 우선순위 — R4 > R3(유효 결정) > R2(현행 버전) > R1(근거 전용).** 이 해석은 LLM이 아니라 코드(`resolve_current`)가 한다. 대체된 결정·구버전은 판정에 들어가지 않는다.

| | 담는 것 | 찾는 방법 |
|---|---|---|
| R1 회의 원문 | 누가 언제 무슨 말을 했나 | 단어 검색(BM25) — 잡담이 많아 의미 검색이 엉뚱한 발언을 붙임 |
| R2 기준 문서 | 지금 무엇을 하기로 되어 있나 | 벡터 |
| R3 결정 원장 | 언제·누가·무엇을·왜·무엇 대신 | 별칭 색인으로 좁힌 뒤 LLM이 번호로 고름 |
| R4 거버넌스 | 사내 정책·아키텍처 원칙·결정 권한표 | 벡터 |

**모델은 고르기만, 판정은 코드, 확정은 사람.**

| 검사 | 하는 일 |
|---|---|
| gate | 모델은 발언 **번호**만 고르고 인용문은 코드가 원문에서 복사. 확정·보류·회상 표현 규칙, 원문에 없는 기한·담당자는 지움 |
| 판정표 | 모델에게는 기준마다 같다·다르다만 묻고, 충돌 종류·승인 권한자는 코드 표가 결정 |
| validate | 답의 결정 번호·숫자·날짜가 근거에 글자 그대로 있는지 대조 + LLM 심판, 최대 3회 재작성 |
| 보류 | 판정 실패·기준 없음·주제 애매 → 단정하지 않고 '확인 필요'로 사람에게 |

결정 찾기에서 벡터를 뺀 이유: "Redis 캐시 쓰기로 했어?"가 벡터 점수로 Redis를 기각한 결정에 붙었고, 점수로 붙은 후보는 왜 붙었는지 설명할 수 없다. 목록 크기별로 재면 통째로 읽기는 무너진다([목록 크기 곡선](eval/list_scaling.md)).

| 결정 목록 | 통째로 읽기 | 색인으로 좁힌 뒤 읽기 |
|---|---|---|
| 50건 | 77% · 5.7초 | 92% · 1.1초 |
| 400건 | 23% · 49초 | 92% · 1.1초 |

## 4. 주요 기능

| 기능 | 내용 |
|---|---|
| 회의 정리 | 결정마다 상태 라벨(확인 필요·승인 대기)·근거 인용·충돌·**해결 방향**(권한표 기준값 비교 → 승인자 안내), 할 일, 미결, 눈여겨볼 점 |
| 근거 질문 | **[확정]·[승인 대기]·[미결]·[확인 필요]·[찾지 못함]** 라벨 + 결정 기록(이유·기각한 대안·원문 인용) + 사안의 흐름 |
| 사실 질문 | "누가 승인했어?", "누가 제안했어?", "언제 회의야?"를 LLM 없이 표 조회로 0.2초 |
| 의견 검토 | 유효 결정·정책·기준 문서와 충돌하는지, 해결 방향. 팀장 승인이 필요하면 요청 내용·사유가 채워진 승인 요청 버튼 |
| 사람 승인 | 팀장만 승인·반려. 승인함에 근거 발언과 **승인하면 대체될 결정** 미리보기, 근거 앞뒤 원문을 보며 고치는 수정 창, 새 주제·별칭은 사람이 만듦(모델이 지은 주제는 버림) |
| 열람 권한 | 회의는 기본적으로 참석자만. 권한 없는 결정은 내용을 LLM에 넘기지 않음. 열람 요청·승인 |
| 품질 감시 | 사람이 고친 흔적(반려·주제 변경)에서 모델이 놓친 것을 찾아 별칭 후보로 → 팀장이 누르면 색인 갱신. 파이프라인 버전·드리프트·승격 게이트([docs/pipeline_ops.md](docs/pipeline_ops.md)) |
| 그 밖에 | 미결함, 일정 조회·추가(초안 → 사람 확인 후 저장), 면접 전사문 정리, 같은 요청 답 재사용 |

## 5. 결과

### 5.1 같은 입력, 세 방식 (회의 3 · 결정 11 · 의견 5 · 질의 6, qwen3:8b)

| 지표 | LLM 단독 | 단순 RAG | 결정 원장 |
|---|---|---|---|
| 결정 재현율 / 정밀도 (%) | 91 / 77 | 82 / 53 | 100 / 100 |
| 할 일 담당·기한 정확 (%) | 11 | 11 | 100 |
| 충돌 탐지 (%) · 오탐 (건) | 0 · 0 | 88 · 2 | 100 · 0 |
| 근거 질의 정답 (%) | 0 | 50 | 100 |
| 평균 처리 시간 (초) · LLM 호출 | 8.5 · 14 | 14.2 · 14 | 24.8 · 63 |

단순 RAG는 대체된 결정을 근거로 오탐했고, LLM 단독은 할 일 기한 연도를 지어냈다. 대가는 처리 시간 약 3배. 이 100%는 규칙을 그 회의 3건에 맞춰 고친 결과라 일반화 성능이 아니다. 재실행: `.venv/bin/python eval/run_eval.py`

### 5.2 처음 보는 회의로 다시 잰 신뢰도 (회의 40 · 결정 200 · 질문 200)

조정용 dev 20과 채점용 test 20으로 나눴고, test는 마지막에 **한 번만** 돌렸다.

| 지표 (괄호는 95% 구간) | dev 기준선 | dev 수정 후 | **test (최종)** |
|---|---|---|---|
| 자동 처리 정확도 | 48.8% | 53.8% | **47.5%** (39.4–55.7) |
| 사람에게 넘긴 것 중 실제로 애매·오류 | 77.2% | 76.4% | **78.8%** (71.1–84.9) |
| 결정 재현율 / 정밀도 | 78.0 / 52.0% | 85.0 / 57.0% | **77.0 / 54.6%** |
| 충돌 탐지 | 24.7% | 32.1% | **24.1%** |
| 응답 시간 중앙값 (회의 / 질문) | 138 / 15.4초 | 184 / 14.4초 | **174 / 11.9초** |

- 자동 처리 정확도가 절반이라 원장에 자동으로 쓰지 않고 **모든 결정이 팀장 승인을 거치는 초안**으로 둔다. 대신 사람에게 넘긴 것의 79%는 실제로 애매하거나 틀린 건이다
- dev에서 고친 규칙이 test에서는 남지 않았다. 병목은 회의 대화에서 결정에 주제를 붙이는 단계(45%) — 한 문장 의견이면 규정 충돌의 76.9%를 잡는다
- 승격 게이트가 실제 회귀를 막았다: dev 수정(689a7d3)이 할 일 기한 정정을 결정으로 뽑게 만들어 결정 정밀도 100 → 92 → 승격 거부 → 수정(377bc2b) 후 통과
- 원본: `eval/reliability/` (`results_compare.md`, `error_analysis.md`)

### 5.3 화면

사진을 누르면 원본 크기로 열립니다.

<table>
<tr><th width="50%">근거 질의 [확정] — 이유·기각 대안·원문·사안의 흐름</th><th width="50%">의견 검토 — 충돌과 해결 방향</th></tr>
<tr><td><a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/sse_flow.jpg"><img src="docs/images/sse_flow.jpg" width="100%" alt="근거 질의"></a></td><td><a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/opinion_question.jpg"><img src="docs/images/opinion_question.jpg" width="100%" alt="의견 검토"></a></td></tr>
<tr><th>관리자 콘솔 승인함 — 근거 발언·대체될 결정</th><th>미결함 — 경과일·후속 할 일</th></tr>
<tr><td><a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/admin_approve.jpg"><img src="docs/images/admin_approve.jpg" width="100%" alt="승인함"></a></td><td><a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/open_items.jpg"><img src="docs/images/open_items.jpg" width="100%" alt="미결함"></a></td></tr>
<tr><th>근거 질의 [승인 대기] → "이거 누가 승인했어?"</th><th>LangSmith 노드별 실행 추적</th></tr>
<tr><td><a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/question_answer.jpg"><img src="docs/images/question_answer.jpg" width="100%" alt="근거 질의 승인 대기"></a></td><td><a href="https://raw.githubusercontent.com/41ways/meeting-decision-ledger/main/docs/images/langsmith_trace.jpg"><img src="docs/images/langsmith_trace.jpg" width="100%" alt="LangSmith"></a></td></tr>
</table>

## 6. 실행 방법 (Mac)

```bash
bash sh/setup_venv.sh
```

```bash
cp .env.example .env
```

```bash
ollama pull qwen3:8b && ollama pull qwen3:0.6b && ollama pull qwen3-embedding:4b
```

```bash
.venv/bin/python scripts/ingest.py
```

```bash
bash sh/run.sh
```

브라우저에서 http://127.0.0.1:8100 — 로그인할 사용자를 고르고 예시 버튼을 누르거나 직접 입력한다. 사용자·회의 등장인물은 모두 가상 인물이다.

시연 상태로 되돌리기(서버를 끈 뒤). 시연 대화방의 답은 실제 실행 결과를 옮겨 둔 목업(`scripts/seed_demo.py`)이고, 새로 질문하면 실제로 처리한다.

```bash
bash scripts/reset_demo.sh
```

테스트:

```bash
.venv/bin/python -m pytest -q tests
```

## 7. 개발 중 문제와 해결 (주요)

| 문제 | 해결 |
|---|---|
| 충돌 목록에 같은 정책이 10번, 유효 결정은 누락 | LLM은 기준별 같다·다르다만, 분류는 코드 판정표 |
| 할 일 기한이 2023년, 없는 담당자 생성 | 원문에 없는 이름·날짜는 비움, 회의 연도로 보정 |
| 판정 출력이 깨지거나 조회가 실패하면 '문제없음' | 실패·빈 판정은 '확인 필요', 기준이 없으면 '비교할 기준 없음' |
| 추출 출력이 잘리면 '결정 0건'으로 조용히 넘어감 | 한 번 더 시도, 두 번 실패면 오류로 드러냄 |
| 단순 RAG가 대체된 결정을 근거로 오탐 | RAG 4종 분리 + 코드가 우선순위 해석 |
| "누가 승인했어?"를 결정 근거 질의로 처리 | 사실 질문 경로(표 조회, LLM 없음) |

## 8. 한계

- **정확도**: 처음 보는 회의에서 자동 처리 정확도 47.5%, 결정 주제 45%. 규칙을 더하면 과적합이 되고, 근본은 더 큰 모델·실제 회의 데이터인데 회의 원문은 외부 LLM에 보낼 수 없다(이름을 가려도 무엇을 정했는지가 기밀) → 사내 GPU 서버
- **화자 분리**: 화자가 적힌 전사문을 가정한다. 확정 권한·제안자를 화자로 판단하므로 STT + 화자 분리 연동이 다음 단계
- **새 프로젝트 세팅**: 주제·별칭·권한표를 사람이 먼저 넣어야 한다. 빠진 표현은 '전체 읽기 알림'과 품질 감시의 별칭 후보로 드러난다
- **교차검증**: 작은 모델 2개의 재검토를 옵션으로 넣었으나 dev에서 정확도가 그대로(53.8 → 54.2%)여서 기본값은 끔
- **처리 시간**: 회의 1건 약 3분(로컬 8b), 질의 평균은 LLM 단독의 약 3배
- **의미 캐시 없음**: 'SSE 왜 골랐어?'와 'SSE 왜 버렸어?'처럼 뜻이 반대인 질문에 저장된 답을 잘못 돌려줄 위험이 있어 같은 문장일 때만 답을 재사용
- **하드코딩**: 프로젝트·사용자 목록(`data/project/*.json`), 확정 표현 정규식, 시연 대화 목업. 로그인은 시연용(비밀번호 없음)

트레이드오프별 기록은 [docs/adr.md](docs/adr.md).

## 문서

- [docs/결과보고서.md](docs/결과보고서.md) — 결과 보고서
- [docs/발표대본.md](docs/발표대본.md) — 5분 발표 대본과 예상 질문
- [docs/design.md](docs/design.md) — 설계서 (RAG 역할·우선순위·데이터 모델·파이프라인·MCP)
- [docs/adr.md](docs/adr.md) — 설계 결정 기록
- [docs/pipeline_ops.md](docs/pipeline_ops.md) — 파이프라인 버전·드리프트·승격·롤백
- [docs/calendar_integration.md](docs/calendar_integration.md) — 구글 팀 달력 연동 준비
- [docs/base_analysis.md](docs/base_analysis.md) — 베이스 에이전트 분석
- [data/project/](data/project/) — 가상 프로젝트 데이터
