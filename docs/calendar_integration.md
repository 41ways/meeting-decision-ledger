# 구글 팀 달력 연동 — 준비 상태와 연결 절차

지금은 **파이프라인만 준비**했다. 실제 구글 API 호출·인증은 없다. 기본 저장소는 로컬 SQLite(`store/calendar.db`)다.

## 1. 지금 있는 것

| 위치 | 내용 |
|---|---|
| `ledger/calendar.py` `CalendarProvider` | 인터페이스: `list_events`, `create_event`, `update_event`, `delete_event` |
| `LocalCalendarProvider` | SQLite `events` 표. 실제로 동작하는 기본 구현 |
| `GoogleCalendarProvider` | 스텁. 필드 매핑(`to_google`·`from_google`), 설정 읽기(`from_env`), 호출 지점(`_request`)만 있다. dry-run이면 보낼 요청을 `sent`에 쌓고 로그만 남기고, dry-run을 끄면 `NotImplementedError` |
| `build_calendar()` | `CALENDAR_PROVIDER=local|google`로 고른다 |
| `LedgerDeps.calendar` | 그래프·서버·MCP는 인터페이스만 본다. 테스트는 가짜 provider를 넣는다 |

환경변수 (`.env.example`에 이름만 있다. 실제 값은 `.env`에만):

| 이름 | 뜻 |
|---|---|
| `CALENDAR_PROVIDER` | `local`(기본) 또는 `google` |
| `CALENDAR_DB` | local일 때 DB 경로 (기본 `store/calendar.db`) |
| `GOOGLE_CALENDAR_ID` | 팀 달력 ID (`xxxx@group.calendar.google.com`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | 서비스 계정 키 JSON 경로 |
| `GOOGLE_CALENDAR_DRY_RUN` | `true`(기본)면 요청을 만들기만 한다 |

## 2. 필드 매핑

| 우리 일정 | Google Event | 비고 |
|---|---|---|
| `title` | `summary` | |
| `date` + `start`·`end` | `start.dateTime`·`end.dateTime` (`Asia/Seoul`) | 구글은 `end`가 필수라 끝이 비면 시작 +1시간으로 보낸다 |
| `date` (시작 시간 없음) | `start.date`·`end.date`(다음 날) | 종일 일정 |
| `attendees` (이름) | `description` + `extendedProperties.private.attendees` | users.json에 이메일이 없어 구글 `attendees`(이메일 필수)에 못 넣는다. 이메일을 붙이면 `attendees`로 옮긴다 |
| `event_id` (EVT-0001) | `extendedProperties.private.ledger_event_id` | 양쪽 ID를 잇는 열쇠 |
| `project`·`kind` | `extendedProperties.private.project`·`kind` | |
| `external_id` | `id` | 구글 이벤트 ID. events 표에 보관 |

## 3. 동기화 방향

**1단계는 단방향(우리 → 구글)을 권한다.**

- 단방향: 원본은 로컬 SQLite. 사람이 '추가'를 누르면 로컬에 저장하고 같은 내용을 구글에 `events.insert`. 구글 쪽에서 고친 내용은 돌아오지 않는다. 구현이 단순하고 "확정은 사람이 화면에서" 원칙과 맞는다.
- 양방향: 구글에서 직접 잡은 일정도 일정 조회 답변·겹침 검사에 들어온다. `events.list`의 `syncToken`으로 바뀐 것만 받아 events 표에 반영하고(`source='google'`), 삭제는 `status='cancelled'`로 온다. 웹훅(`events.watch`)을 쓰려면 공개 HTTPS 주소가 필요하다.

## 4. 이벤트 ID 매핑

- 로컬 → 구글: insert 응답의 `id`를 `external_id`에 저장. `extendedProperties.private.ledger_event_id`에도 우리 ID를 넣어 두면 응답을 잃어도 다시 찾을 수 있다 (`privateExtendedProperty=ledger_event_id=EVT-0001`로 검색).
- 구글 → 로컬: `ledger_event_id`가 없는 이벤트는 구글에서 직접 만든 것. 새 로컬 ID를 붙이고 `source='google'`.
- 같은 요청이 두 번 가는 것을 막으려면 구글 이벤트 `id`를 우리가 정해서 보낸다 (base32hex 5~1024자, 예: `evt0001` 소문자). 재시도해도 409로 끝나 중복이 생기지 않는다.

## 5. 충돌 처리

- 시간 겹침(같은 참석자): 지금처럼 저장 전에 경고하고 사람이 고른다. 구글 일정까지 보려면 겹침 검사 때 `list_events`를 구글에서도 읽거나 `freeBusy.query`를 쓴다.
- 수정 충돌(양방향일 때): 구글 `etag`를 저장해 두고 `If-Match`로 수정. 412가 오면 구글 쪽이 먼저 바뀐 것이므로 덮어쓰지 않고 화면에 알린다. 기준은 "나중에 바꾼 쪽 우선"이 아니라 "사람이 확인".
- 실패: 구글 호출이 실패해도 로컬 저장은 유지하고 `external_id`가 빈 일정을 '미동기화'로 표시, 다음 실행 때 다시 보낸다 (타임아웃만 재시도하는 `with_retry` 규칙과 같음).

## 6. 인증 — 서비스 계정 vs 사용자 OAuth

| | 서비스 계정 | 사용자 OAuth |
|---|---|---|
| 맞는 경우 | 서버가 팀 공용 달력 하나에 쓴다 (이 프로젝트) | 각자 개인 달력에 쓴다 |
| 준비 | GCP 프로젝트 → Calendar API 사용 설정 → 서비스 계정 → 키 JSON. 팀 달력 공유 설정에 서비스 계정 이메일을 "일정 변경" 권한으로 추가 | OAuth 동의 화면, 클라이언트 ID, 사용자별 토큰 저장·갱신 |
| 주의 | 키 파일은 `.env` 경로로만 가리키고 저장소에 올리지 않는다. 참석자 초대 메일을 보내려면 Workspace 도메인 전체 위임이 필요 | 토큰 저장소 보안, 동의 철회 처리 |

필요한 스코프는 `https://www.googleapis.com/auth/calendar.events` 하나(이벤트 읽기·쓰기). 달력 자체 설정을 바꾸는 `calendar` 전체 스코프는 쓰지 않는다.

## 7. 팀 달력 공유 설정

1. 구글 캘린더에서 팀 달력을 만든다 (개인 기본 달력이 아니라 별도 달력).
2. 설정 → 특정 사용자와 공유 → 서비스 계정 이메일을 "일정 변경 및 공유 관리"가 아닌 **"일정 변경"**으로 추가.
3. 설정 → 캘린더 통합 → 캘린더 ID를 `GOOGLE_CALENDAR_ID`에 넣는다.
4. 팀원에게는 같은 달력을 "모든 일정 세부정보 보기"로 공유.

## 8. 실제 연결 때 할 일 (체크리스트)

- [ ] GCP 프로젝트 생성, Calendar API 사용 설정
- [ ] 서비스 계정 생성, 키 JSON 발급 → 저장소 밖 경로에 두고 `.env`의 `GOOGLE_APPLICATION_CREDENTIALS`에 경로만
- [ ] 팀 달력 생성, 서비스 계정에 "일정 변경" 공유, `GOOGLE_CALENDAR_ID` 설정
- [ ] `requirements.txt`에 `google-auth`(토큰 발급) 추가. HTTP는 이미 쓰는 `httpx`로
- [ ] `GoogleCalendarProvider._request`를 실제 호출로 구현 (토큰 갱신, 타임아웃 재시도, 4xx는 즉시 실패)
- [ ] 원본을 로컬로 두는 단방향이면 `CALENDAR_PROVIDER=local`을 유지하고, 저장 뒤 구글로 보내는 동기화 단계를 추가 (로컬 저장 성공 → 구글 insert → `external_id` 갱신)
- [ ] users.json에 이메일 필드를 붙여 구글 `attendees`로 매핑할지 결정 (초대 메일 발송 여부와 함께)
- [ ] events 표에 `etag`·`synced_at` 열 추가 (양방향일 때)
- [ ] dry-run으로 `sent` 요청 본문을 확인한 뒤 `GOOGLE_CALENDAR_DRY_RUN=false`
- [ ] 테스트 달력에서 추가·수정·삭제·중복 재시도(409) 확인 후 팀 달력으로 전환
