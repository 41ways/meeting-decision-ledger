#!/usr/bin/env bash
# 시연 상태 초기화: 원장·RAG를 시드로 되돌리고 대화방·권한·알림을 비운다. 서버를 끈 상태에서 실행.
set -euo pipefail
cd "$(dirname "$0")/.."
./.venv/bin/python scripts/ingest.py
# 회의를 날짜순으로 실제 정리해 회의 기록(미결·할 일·요약·사안)을 채운다 (로컬 LLM, 회의당 1분 안팎). 건너뛰려면 SKIP_PROCESS=1
if [ "${SKIP_PROCESS:-0}" != "1" ]; then ./.venv/bin/python scripts/process_meetings.py; fi
rm -f store/access.db session.db
./.venv/bin/python scripts/seed_demo.py     # 시연용 대화방·요청 목업
echo "초기화 완료 — bash sh/run.sh 로 다시 실행"
