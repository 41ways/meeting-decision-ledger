# 신뢰도 검증 데이터셋 작성 기준

회의 전사문과 정답을 같은 설계에서 함께 쓴다. 정답은 사람이 설계한 시나리오에서 나오며, 실행 모델(qwen)로 만들지 않는다.
작성 뒤 `.venv/bin/python eval/reliability/dataset.py --check`가 오류 0건이어야 한다.

## 1. 세계

- 회사 정책: `data/project/r4_governance` 현행 v3 + `world/r4_governance/policies.json`(SEC-04 위치정보, SEC-05 단말 저장, FIN-01 증빙 5년, DATA-01 DW 원천). 구 정책 v2에는 ARC-01(MySQL 표준), FIN-01(3년) 같은 함정이 있다.
- 예외: W-001(OPS, SEC-01, 마스킹 로그 샘플 200건 PoC, ~2026-11-30), W-003(FLD, SEC-04, 시범 사용자 20명 GPS 10분 간격, ~2026-12-31), W-004(SLS, SEC-03, Superset Cloud 체험판·마스킹 샘플만, ~2026-11-27), W-005(EXP, ARC-01, OCR 번들 MySQL을 인식 결과 7일 임시 저장만, ~2027-03-31).
- 권한표(AUTH): 기술 선택 PL(AUTH-01) · 범위·요구사항·성능 기준 PM(AUTH-02) · 일정 지연 1~2주 PM, 2주 이상 본부장(AUTH-03) · 비용 월 500만 원 이하 PM, 초과 본부장(AUTH-04) · 보안 영향 보안 담당(AUTH-05). PostgreSQL 외 DB는 아키텍처 위원회(ARC-01), 운영 반영은 CAB(OPS-01), 외부 SaaS는 보안성 검토(SEC-03).
- 프로젝트 4개: OPS(기존, 회의 OPS-M11~M20), EXP·FLD·SLS(새로, 회의 M01~M10). 프로젝트별 인물·주제·기준 문서·시드 결정은 아래 4절.
- 시간: 10주. 1~5주 = 개발 셋, 6~10주 = 시험 셋. 같은 주에 OPS 월, EXP 화, FLD 수, SLS 목 회의.

| 주 | OPS | EXP | FLD | SLS | 분할 |
|---|---|---|---|---|---|
| 1 | M11 10-26 | M01 10-27 | M01 10-28 | M01 10-29 | dev |
| 2 | M12 11-02 | M02 11-03 | M02 11-04 | M02 11-05 | dev |
| 3 | M13 11-09 | M03 11-10 | M03 11-11 | M03 11-12 | dev |
| 4 | M14 11-16 | M04 11-17 | M04 11-18 | M04 11-19 | dev |
| 5 | M15 11-23 | M05 11-24 | M05 11-25 | M05 11-26 | dev |
| 6 | M16 11-30 | M06 12-01 | M06 12-02 | M06 12-03 | test |
| 7 | M17 12-07 | M07 12-08 | M07 12-09 | M07 12-10 | test |
| 8 | M18 12-14 | M08 12-15 | M08 12-16 | M08 12-17 | test |
| 9 | M19 12-21 | M09 12-22 | M09 12-23 | M09 12-24 | test |
| 10 | M20 12-28 | M10 12-29 | M10 12-30 | M10 12-31 | test |

## 2. 원장 규칙 (정답이 따르는 것)

- 회의가 끝나면 팀장이 결정마다 `outcome`대로 처리했다고 가정한다: `approved`(승인, 시행일 = 회의 날짜) / `rejected`(반려) / `pending`(승인 대기로 둠) / `confirms`(앞서 승인 대기였던 결정을 이 발언으로 확정 → 그 결정을 승인, 새 번호 없음).
- 승인하면 **같은 프로젝트·같은 주제 키**의 기존 유효 결정은 대체된다. 별개 사안이면 다른 주제 키를 고른다.
- 결정 번호는 시뮬레이션이 발급하므로 정답에서는 `{EXP-M03-D2}`처럼 정답 ID로 가리킨다 (질문 문장 안에서도).
- 충돌 판단 기준 (사람 검토자가 짚어야 하는 것):
  - `policy`: 현행 R4 조항(AUTH 제외)을 어기고, 그 프로젝트·회의 날짜에 유효하고 범위가 맞는 예외가 없다. `against`에 규칙 ID.
  - `authority`: 결정한 사람에게 권한이 없고 권한자도 참석하지 않았다(또는 금액·기간 기준을 넘어 상위 권한자가 필요). `against`에 AUTH-0x.
  - `past_decision`: 같은 프로젝트·같은 주제의 **회의 시점 유효 결정**과 모순되고 권한자의 정당한 변경이 아니다. `against`에 결정 ID(시드 ID 또는 `{정답 ID}`).
  - `baseline`: 그 프로젝트 현행 기준 문서 조항과 다르고 정당한 변경이 아니다. `against`에 조항 번호(EREQ-03 등).
  - `change_approved`: 권한자가 기존 결정·기준 문서를 바꾸는 정당한 결정 (다른 막는 충돌 없음). 막는 충돌이 아니다.
  - `confirms`: 승인 대기 결정을 확정하는 발언. `against`에 그 결정.
  - `past_project`: 다른 프로젝트의 유효 결정과 다름 — 참고일 뿐 막는 충돌이 아니다.
- `expected_label`: 막는 충돌(policy·authority·past_decision·baseline)이 하나라도 있으면 `확인 필요`, 없으면 `승인 대기`. 사람마다 판단이 갈릴 결정(조건부 확정 등)은 `ambiguous: true`.
- 막는 충돌이 있는 결정의 outcome은 `rejected`(대부분) 또는 `pending`.

## 3. 회의 전사문 (`meetings/<회의 ID>.json`)

`data/project/r1_meetings`와 같은 형식: `meeting_id, project_id, date, title, attendees, segments[{seq, ts "HH:MM", speaker, role, text}]`.
발언 25~40개, seq는 1부터 연속, role은 인물 목록의 speaker_role. 결정 5건 안팎, 할 일 2~3건, 미결 1~2건, 함정 2~4개.

실제 회의처럼 쓴다 — 시스템 규칙(확정 표현 목록 등)에 맞춰 쓰지 않는다. 회의마다 섞을 것:
잡담 · 주제 이탈 · 말 끊김(문장이 끊기고 다른 사람이 이어 말함) · STT 오인식(용어집 'STT 오인식 예' 표기를 발언에 그대로) ·
돌려 말한 결정('그 방향으로 가시죠', '일단 그렇게 해 보죠', '그걸로 하시죠') · 확정 표현 없는 진행자 단정('인증은 SSO로 붙입니다') ·
회상('지난번에 정했죠', '예전에 ~했었는데') · 보류('다음 주에 다시 보죠') · 조건부 확정('예산 승인 나면 그걸로 갑시다') ·
번복(같은 회의에서 정했다가 뒤집음 — 앞 것은 함정, 뒤 것이 결정) · 회의 밖 결정 언급('본부장님이 메일로 승인하셨대요' — 이 회의의 결정 아님) ·
권한 밖 결정 · 같은 회의 안 기한 정정('17일 말고 20일로 하죠') · 요청자≠담당자.

**문제 행동(권한 위반·규정 위반 제안이나 확정)은 데이터셋 가상 인물만 한다.** 기존 인물(정한결·서지후·장상민·노영오·김태림·황민채·한소율·윤하린)은 정상 행동만 — 위반 제안·확정 발언의 화자가 될 수 없다 (검사가 막는다).

## 4. 정답 (`gold/<프로젝트 약자>.json`)

```json
{
 "project": "EXP",
 "meetings": {
  "EXP-M01": {
   "split": "dev",
   "phenomena": ["잡담", "STT 오인식", "돌려 말한 결정", "..."],
   "decisions": [
    {"id": "EXP-M01-D1", "seq": 12, "evidence_seq": [10, 12], "topic": "exp.ocr",
     "statement": "OCR 인식 실패 건은 수기 입력 화면으로 넘긴다", "decided_by": "석다은",
     "rationale": "인식률 90% 미만 영수증 대비", "alternatives_rejected": ["재촬영 요청"],
     "conflicts": [{"type": "baseline", "against": ["EREQ-01"], "detail": "..."}],
     "expected_label": "확인 필요", "ambiguous": false, "outcome": "rejected",
     "tags": ["진행자 단정"]}
   ],
   "action_items": [
    {"id": "EXP-M01-A1", "seq": 20, "evidence_seq": [20, 21], "statement": "카드사 API 키 발급 요청",
     "owner": "편도훈", "due": "2026-11-03", "requester": "금보람", "tags": ["요청자≠담당자"]}
   ],
   "undecided": [
    {"id": "EXP-M01-U1", "seq": 15, "evidence_seq": [14, 15], "subject": "결재 알림 메신저 추가", "topic": "exp.notify", "tags": ["보류"]}
   ],
   "traps": [{"seq": 7, "kind": "회상", "why": "지난 회의 결정을 다시 말함 — 새 결정 아님"}]
  }
 },
 "opinions": [
  {"id": "EXP-O01", "split": "dev", "at": "EXP-M02", "scope_project": "EXP",
   "text": "영수증 원본은 1년만 두고 지우면 어떨까요?", "expected": "conflict",
   "against": ["FIN-01", "EREQ-06"], "waiver": null, "ambiguous": false, "note": "..."}
 ],
 "questions": [
  {"id": "EXP-Q01", "split": "dev", "at": "EXP-M03", "scope_project": "EXP",
   "text": "OCR은 왜 DocuRead로 했어요?", "kind": "why", "follow_up_of": null,
   "expected_state": "확정", "expected_ids": ["EXP-0002"], "expected_keywords": ["DocuRead"],
   "forbidden_keywords": [], "ambiguous": false, "note": "..."}
 ]
}
```

- 결정: `seq` = 확정 발언, `evidence_seq` = 제안 발언(확정 3발언 이내)과 확정 발언. `decided_by` = 확정 발언 화자. `rationale`·`alternatives_rejected`는 원장에 남는 이유(질문 답의 근거).
- 할 일: `seq` = 배정 발언, `evidence_seq` = 배정·수락·정정 발언. 원문에 담당·기한이 없으면 `null` (지어내지 않는다). 기한 정정이 있으면 마지막 날짜. '다음 주 금요일'처럼 상대 표현은 실제 날짜로.
- 미결: 논의했지만 확정하지 않은 사안. `topic` 필수(나중 결정과 이어진다).
- 함정 `kind`: 회상 · 제안 · 보류 · 번복 전 결정 · 회의 밖 결정 · 잡담 · 수락 · 질문 중 하나 — 결정으로 뽑으면 틀린 발언.
- 의견 `at` = 이 회의를 처리한 뒤 묻는다(그 시점 원장 기준). `scope_project` = 화면에서 고른 프로젝트(안 골랐으면 null). `expected` = conflict / allowed_with_waiver / no_conflict / needs_approval(사람이 봐야 하는 애매한 의견).
- 질문 `expected_state` = 확정 / 승인 대기 / 미결 / 찾지 못함. `kind` = why · status · value · fact · cross_project · authority · follow_up · trap.
  - 확정: `expected_ids` 중 하나 또는 `expected_keywords` 전부가 답에 있어야 정답. 지난 값(대체된 값)은 `forbidden_keywords`.
  - 승인 대기: 그 결정 ID와 '승인 대기'. 미결: `expected_ids`에 미결 정답 ID(`{EXP-M02-U1}`). 찾지 못함: `expected_ids` 비움.
  - `follow_up_of`: 같은 시점에 먼저 묻는 질문 ID. '이거 누가 승인했어요?', '그건 왜 그렇게 정했어요?'처럼 앞 답을 가리킨다.
  - authority: 권한 밖에서 정해져 반려·대기된 결정을 묻는다 (정답은 여전히 유효한 이전 결정, 또는 승인 대기).

## 5. 프로젝트별 규모 (분할마다)

| 프로젝트 | 결정 dev/test | 할 일 dev/test | 의견 dev/test | 질문 dev/test | 찾지 못함 dev/test |
|---|---|---|---|---|---|
| OPS | 25 / 25 | 12 / 13 | 7 / 8 | 25 / 25 | 5 / 5 |
| EXP | 25 / 25 | 13 / 12 | 8 / 7 | 25 / 25 | 5 / 5 |
| FLD | 25 / 25 | 12 / 13 | 7 / 8 | 25 / 25 | 5 / 5 |
| SLS | 25 / 25 | 13 / 12 | 8 / 7 | 25 / 25 | 5 / 5 |

분할마다(프로젝트당 25건) 결정 구성: 막는 충돌 8~10건(policy ≥2, authority ≥2, past_decision ≥2, baseline ≥1), change_approved 1~2건, 10주 안에 confirms ≥1건, 나머지는 충돌 없음.
현상: 돌려 말한 결정 ≥3, 진행자 단정 ≥2, 조건부 확정 ≥2, 번복 ≥1, 권한 밖 결정 ≥2, STT 오인식 ≥3, 회상 함정 ≥2, 회의 밖 결정 함정 ≥2, 보류(미결) ≥3, 기한 정정 ≥2, 요청자≠담당자 ≥2.
질문(25): 찾지 못함 5(그중 ≥2는 비슷한 말이 있는 어려운 함정이나 다른 프로젝트에만 있는 결정), 확정 ~9(대체된 뒤 '지금 기준' ≥2, authority ≥1), 승인 대기 ~4, 미결 ~4, follow_up 2, cross_project ≥2, scope_project null ~5.
의견(7~8): conflict ~3, allowed_with_waiver 1~2(예외가 있는 프로젝트·기간 안), no_conflict ~2, needs_approval ~1.

## 6. 프로젝트 요약

### OPS — PRJ-OPSAI 운영 장애 대응 AI (기존 세계에 이어짐)
- 인물: 정한결 PM · 서지후 PL · 장상민 백엔드 · 노영오 AI 개발 · 김태림 보안 · 한소율 운영팀 · 황민채 기획 · 윤하린 인턴 (기존 — 정상 행동만), 마태수 협력사 PM · 도재윤 프론트엔드 · 권유나 QA · 피승우 운영팀 (가상 — 문제 행동 가능).
- 유효 결정(10-26 시점): OPS-0001 FastAPI·LangGraph, 0002 MCP stdio, 0004 사내 Ollama(외부 API 보안 승인 전 제외), 0006 Supervisor, 0007 SSE, 0008 PostgreSQL 대화 저장, 0009 simple/complex 라우팅, 0010 모델 티어링, 0011 검증 노드 재작성 1회, 0012 권위 게이트, 0013 타임아웃만 재시도, 0014 캐시는 런북만, 0018 최종 답변 60초, 0020 회의 원문 90일. 반려: 0015 WebSocket, 0016 2주 연기, 0017 운영 로그 원문 외부 비교, 0019 MySQL.
- 기준 문서 REQ v1.2(REQ-01~08, REQ-06 운영 반영 2026-12-15 CAB 후), 아키텍처 개요서 v1.0. 예외 W-001(~11-30).
- 이야기 줄기: 운영 반영 준비(12-15, CAB), 장애 알림 연동(ops.alert), 운영자 피드백(ops.feedback), QA(qa.test), 반영 뒤 안정화.

### EXP — PRJ-EXPAUTO 경비 정산 자동화 (오픈 2027-02-01)
- 인물: 남궁현 PM · 석다은 PL · 편도훈 백엔드 · 여민아 프론트엔드 · 금보람 재무팀 · 육성재 보안 · 방규태 OCR 협력사 PM · 차윤호 백엔드.
- 시드: EXP-0001 Spring Boot, 0002 OCR은 사내 DocuRead, 0003 PostgreSQL, 0004 ERP 야간 배치 1회, 0005 외부 솔루션 월 300만 원 이내, 0006 결재 알림 메신저 추가(승인 대기), 0007 100만 원 이상 2단계 결재.
- 기준 문서 EREQ v1.1(EREQ-01~08; v1.0은 결재 1단계 — 구버전 함정). 예외 W-005. 주제: exp.ocr · exp.approval · exp.erp · exp.card · exp.notify · exp.mileage · receipt.retention + 공통 키.
- 함정 소재: 외부 OCR SaaS(SEC-03·비용), 영수증 이미지 외부 전송(SEC-01), 증빙 3년 보관(구 FIN-01), OCR 번들 MySQL에 정산 데이터(W-005 범위 밖), 오픈 연기.

### FLD — PRJ-FIELDAPP 현장 설비 점검 앱 (시범 2026-12-07, 정식 2027-01-11)
- 인물: 표지환 PM · 은서율 PL · 맹주원 모바일 · 소하늘 백엔드 · 길봉수 현장팀 · 육성재 보안 · 진태오 협력사 모바일 · 국하윤 디자인.
- 시드: FLD-0001 Flutter, 0002 SSO(OIDC), 0003 암호화 SQLite 오프라인, 0004 사내 푸시 게이트웨이, 0005 GPS 상시 수집 안 함·QR 위치.
- 기준 문서 FREQ v1.0(FREQ-01~08). 예외 W-003(시범 사용자 20명 GPS). 주제: fld.framework · fld.offline · fld.push · fld.photo · fld.auth · fld.checklist · privacy.location · mobile.storage + 공통 키(realtime.transport, schedule …).
- 함정 소재: GPS 전 직원 상시 수집(SEC-04, W-003 범위 밖), 사진 단말 보관(SEC-05), WebSocket 실시간(ARC-02), iOS 지원 범위 추가(AUTH-02), 시범 3주 연기(AUTH-03 본부장).

### SLS — PRJ-SALESDASH 영업 실적 대시보드 (오픈 2026-12-21)
- 인물: 용채원 PM · 탁현승 PL · 설유진 데이터 · 봉민석 프론트엔드 · 엄지수 영업기획 · 육성재 보안 · 반재경 영업본부 · 독고준 협력사 BI 컨설턴트.
- 시드: SLS-0001 사내 Tableau Server, 0002 매일 07시 배치, 0003 집계 마트 PostgreSQL, 0004 라이선스 월 200만 원 이내.
- 기준 문서 SREQ v1.0(SREQ-01~08). 예외 W-004(Superset Cloud 체험판·마스킹 샘플, ~11-27). 주제: sls.bi_tool · sls.refresh · sls.metric · sls.access · sls.export · data.source + 공통 키(llm.external, saas.new …).
- 함정 소재: 영업 엑셀 수기 집계를 공식 수치로(DATA-01), 고객 데이터를 외부 LLM 요약(SEC-01), Superset Cloud 실데이터(W-004 범위·기간 밖), 전원 전체 실적 열람(SREQ-03), 라이선스 월 700만 원(AUTH-04).
