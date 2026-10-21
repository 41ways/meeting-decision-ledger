"""시연 대화방의 답을 지금 코드로 실제로 받아 scripts/demo_outputs.json에 저장한다.

seed_demo.py는 이 파일이 있으면 대화방 답으로 이것을 쓰고, 없으면 파일 안의 예전 목업 상수를 쓴다.
발표에서 로컬 모델로 회의 정리를 기다리면 1분이 넘어서, 미리 실제로 돌린 결과를 대화방에 넣어 두려는 용도다.

순서 (로컬 LLM을 쓰므로 다른 측정과 겹치지 않을 때):
    bash scripts/reset_demo.sh                 # 원장·색인·회의 기록 초기화 + 회의 5개 정리 + 시드(OPS-0011 승인 대기)
    bash sh/run.sh                             # 서버 켜기 (8100)
    .venv/bin/python scripts/capture_demo.py   # 질문마다 서버에 실제로 묻기 (7개, 5분 안팎)
    .venv/bin/python scripts/seed_demo.py      # 대화방을 방금 받은 답으로 다시 채우기

서버의 /chat/stream으로 묻기 때문에 화면에서 묻는 것과 같은 경로(권한·범위·캐시 무시 force)로 돈다.
임시 대화방(cap-*)은 끝나면 지운다. 원장에 새 승인 대기 결정이 생기면(같은 근거의 기존 건은 재사용) 경고로 알린다.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledger.store import LedgerStore  # noqa: E402

BASE = "http://127.0.0.1:8100"
OUT = ROOT / "scripts" / "demo_outputs.json"

# (키, 사용자, 질문, 프로젝트 범위, 회의 범위) — 회의 정리는 담당자 서지후(pdy)가 한 것으로 둔다 (승인 요청자가 서지후)
SPECS = [
    ("m04", "pdy", "OPS-M04 회의 정리해줘", "OPS", "OPS-M04"),
    ("sse", "kjh", "SSE는 왜 골랐어?", "OPS", "OPS-M02"),
    ("i01", "kjh", "HBE-I01 면접 요약해줘", "HBE", "HBE-I01"),
    ("redis_session", "kjh", "세션 저장소를 Redis로 바꾸면 어때?", "OPS", ""),
    ("csb", "kjh", "예전 고객센터 챗봇에서는 왜 웹소켓을 그만뒀어요?", "CSB", ""),
    ("cache", "kjh", "캐시는 어떻게 하기로 했어?", "OPS", "OPS-M03"),
    ("redis_cache", "kjh", "Redis 캐시 쓰기로 했어?", "OPS", ""),
]


def ask(key: str, user: str, message: str, project: str, meeting: str) -> dict:
    """
    서버에 한 번 묻고 최종 답을 받는다
    Args:
        key:     결과 키 (임시 대화방 이름에 씀)
        user:    사용자 ID
        message: 질문
        project: 프로젝트 범위 (약자, 없으면 빈 문자열)
        meeting: 회의 범위 (없으면 빈 문자열)
    Returns:
        {"message", "user", "reply", "mode", "sec", "full_read"}
    Raises:
        RuntimeError: 최종 답 이벤트가 오지 않았을 때
    """
    params = {"message": message, "thread_id": f"cap-{key}", "user": user, "force": "true",
              "project": project, "meeting": meeting, "model": "auto"}
    t0, final, full_read = time.perf_counter(), None, False
    with httpx.stream("GET", f"{BASE}/chat/stream", params=params, timeout=httpx.Timeout(900, connect=10)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev.get("type") == "FullReadFallback":
                full_read = True
            if ev.get("type") == "FinalAnswer":
                final = ev
    if not final:
        raise RuntimeError(f"{key}: 최종 답을 받지 못함")
    return {"message": message, "user": user, "reply": final["reply"], "mode": final.get("mode"),
            "sec": round(time.perf_counter() - t0, 1), "full_read": full_read}


def cleanup() -> None:
    """임시 대화방(cap-*)의 대화와 목록 행을 지운다"""
    with sqlite3.connect(ROOT / "session.db") as c:
        c.execute("DELETE FROM messages WHERE thread_id LIKE 'cap-%'")
    acc = ROOT / "store" / "access.db"
    if acc.exists():
        with sqlite3.connect(acc) as c:
            for table in ("threads", "thread_refs"):
                try:
                    c.execute(f"DELETE FROM {table} WHERE thread_id LIKE 'cap-%'")
                except sqlite3.OperationalError:
                    pass


def main() -> None:
    """질문마다 묻고 결과를 저장한다. 일부 키만: capture_demo.py m04 cache"""
    try:
        httpx.get(f"{BASE}/health", timeout=5).raise_for_status()
    except httpx.HTTPError:
        sys.exit("서버가 꺼져 있습니다. bash sh/run.sh 로 켠 뒤 다시 실행하세요.")
    only = set(sys.argv[1:])
    before = {d["decision_id"] for d in LedgerStore().decisions(project_id="PRJ-OPSAI")}
    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    try:
        for key, user, msg, project, meeting in SPECS:
            if only and key not in only:
                continue
            r = ask(key, user, msg, project, meeting)
            out[key] = {**r, "captured_at": datetime.now().isoformat(timespec="seconds"), "commit": commit}
            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")   # 하나 끝날 때마다 저장
            print(f"{key}: {r['mode']} · {r['sec']}초" + (" · 전체 읽기" if r["full_read"] else ""), flush=True)
    finally:
        cleanup()
    new = [d for d in LedgerStore().decisions(project_id="PRJ-OPSAI") if d["decision_id"] not in before]
    if new:
        print("경고: 캡처 중 원장에 새 승인 대기 결정이 생겼습니다 —",
              ", ".join(f"{d['decision_id']}({d['topic']})" for d in new), "· 시연 전 reset_demo로 되돌리세요")
    print(f"저장: {OUT.relative_to(ROOT)} — 이제 seed_demo.py를 실행하세요")


if __name__ == "__main__":
    main()
