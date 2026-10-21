"""회의를 날짜순으로 실제 정리해 회의 기록(항목·사안·요약)을 채운다 — 시연 준비용.

사용: .venv/bin/python scripts/process_meetings.py [회의 ID …]   (없으면 원문이 있는 OPS 회의 전부)

원장에는 올리지 않는다(propose_to_ledger=False). 시연 목업이 가리키는 승인 대기 결정 번호(OPS-0011)를
seed_demo.py가 정해진 번호로 올리고 이 기록에 잇는다. 로컬 LLM으로 회의당 1분 안팎 걸린다.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()   # ledger.llm이 import 시점에 OLLAMA_URL 등을 읽는다

from agent.crosscheck import build_checkers  # noqa: E402
from agent.ledger_graph import LedgerDeps, build_ledger_graph  # noqa: E402
from ledger.llm import build_llms  # noqa: E402
from ledger.mcp_bridge import LedgerMCP  # noqa: E402
from ledger.metrics import MetricsStore, extract  # noqa: E402
from ledger.pipeline_registry import version_of  # noqa: E402


def meeting_ids(args: list[str]) -> list[str]:
    """
    정리할 회의 ID 목록 (날짜순)
    Args:
        args: 명령행에서 받은 회의 ID (없으면 원문이 있는 OPS 회의 전부)
    Returns:
        회의 ID 목록
    """
    if args:
        return args
    rows = [json.loads(f.read_text(encoding="utf-8")) for f in (ROOT / "data/project/r1_meetings").glob("OPS-M*.json")]
    return [m["meeting_id"] for m in sorted(rows, key=lambda m: m["date"])]


async def main(ids: list[str]) -> None:
    """
    회의마다 그래프의 회의 정리 경로를 돌려 기록을 남긴다
    Args:
        ids: 회의 ID 목록 (앞선 회의의 미결이 뒤 회의 결정에 이어지도록 날짜순)
    """
    fast, quality = build_llms()
    mcp = LedgerMCP()
    await mcp.start()
    try:
        deps = LedgerDeps(fast_llm=fast, quality_llm=quality, mcp=mcp, propose_to_ledger=False,
                          checkers=build_checkers())
        graph, metrics, ver = build_ledger_graph(deps), MetricsStore(), version_of(deps)
        for mid in ids:
            t0 = time.perf_counter()
            try:
                st = await graph.ainvoke({"message": f"{mid} 회의 정리해줘", "user_id": "agent", "history": [], "events": []})
            except RuntimeError as e:          # 추출 실패는 그 회의만 건너뛰고 눈에 띄게 남긴다
                print(f"{mid}: 실패 — {e}", flush=True)
                continue
            metrics.record(extract(st, time.perf_counter() - t0, ver, "process", "agent"))   # 감시용 기록 (동작 무관)
            rep = st.get("report") or {}
            print(f"{mid}: 결정 {len(rep.get('items', []))} · 미결 {len(rep.get('undecided', []))} · "
                  f"할 일 {len(rep.get('action_items', []))} · 이어진 미결 {len(rep.get('linked', []))} "
                  f"({time.perf_counter() - t0:.0f}s)", flush=True)
    finally:
        await mcp.stop()


if __name__ == "__main__":
    asyncio.run(main(meeting_ids(sys.argv[1:])))
