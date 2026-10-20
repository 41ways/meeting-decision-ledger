#!/usr/bin/env bash
# 결정 원장 Agent 서버 실행 → 브라우저 http://127.0.0.1:8100
# 원장을 처음 상태로 되돌리려면: .venv/bin/python scripts/ingest.py
set -euo pipefail
cd "$(dirname "$0")/.."
curl -s http://127.0.0.1:11434/api/tags >/dev/null || { echo "Ollama가 꺼져 있습니다: ollama serve"; exit 1; }
[ -f store/ledger.db ] || ./.venv/bin/python scripts/ingest.py
exec ./.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8100
