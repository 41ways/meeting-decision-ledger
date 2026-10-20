#!/usr/bin/env bash
# 프로젝트 전용 .venv를 만들고 requirements.txt를 설치한다 (베이스 스크립트와 같은 방식).
# activate 대신 .venv 안의 python3를 직접 호출해 전역 설치를 막는다.
#   bash sh/setup_venv.sh
set -euo pipefail
cd "$(dirname "$0")/.."
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3가 없습니다."; exit 1; }
[ -x ".venv/bin/python3" ] || python3 -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip --quiet
./.venv/bin/python3 -m pip install -r requirements.txt
./.venv/bin/python3 -c "import fastapi, uvicorn, langgraph, langsmith, fastmcp, mcp, httpx, dotenv, chromadb; print('패키지 확인 완료')"
cat <<EOF

다음 순서로 실행:
  ollama pull qwen3:8b && ollama pull qwen3-embedding:4b
  .venv/bin/python scripts/ingest.py      # 원장(SQLite)·검색(Chroma) 적재
  bash sh/run.sh                          # http://127.0.0.1:8100
EOF
