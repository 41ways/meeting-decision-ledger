"""그래프를 한 번 실행해 결과를 출력한다. 사용: python scripts/run_graph.py "M04 회의 정리해줘" [out.json]"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()   # ledger.llm이 import 시점에 OLLAMA_URL·NUM_CTX 등을 읽으므로 그보다 먼저
from agent.ledger_graph import LedgerDeps, build_ledger_graph  # noqa: E402
from ledger.llm import build_llms  # noqa: E402
from ledger.mcp_bridge import LedgerMCP  # noqa: E402


async def main(msg: str, out: str | None):
    """
    그래프를 한 번 실행해 최종 답변과 소요 시간·사용량·MCP 호출 수를 출력
    Args:
        msg: 사용자 입력 (예: "M04 회의 정리해줘")
        out: 결과 JSON 저장 경로. None이면 저장하지 않음
    """
    fast, quality = build_llms()
    mcp = LedgerMCP()
    await mcp.start()
    try:                # 실행이 실패해도 MCP 자식 프로세스는 반드시 종료
        graph = build_ledger_graph(LedgerDeps(fast_llm=fast, quality_llm=quality, mcp=mcp))
        t0 = time.perf_counter()
        state = await graph.ainvoke({"message": msg, "thread_id": "cli", "history": []})
        sec = round(time.perf_counter() - t0, 1)
    finally:
        await mcp.stop()
    print(state.get("final", ""))
    print(f"\n-- {sec}s, usage={state.get('usage')}, mcp_calls={mcp.calls}")
    if out:
        Path(out).write_text(json.dumps({k: state.get(k) for k in ("mode", "report", "final", "events", "usage")},
                                        ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
