"""결정 원장 MCP 서버(mcp_ledger_server.py) 클라이언트.

서버 수명 동안 stdio 자식 프로세스 1개를 띄워 세션을 유지한다(결정 D-002, lifespan 싱글턴).
일시 오류(timeout)만 재시도한다(with_retry).
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from langsmith import traceable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.retry import with_retry

ROOT = Path(__file__).resolve().parent.parent


class LedgerMCP:
    def __init__(self, server_path: Path = ROOT / "mcp_ledger_server.py"):
        self.server_path = server_path
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.calls = 0

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        # 환경변수를 그대로 넘긴다 (stdio 기본값은 PATH 등 일부만 전달 — 평가용 LEDGER_STORE, OLLAMA_URL이 빠짐)
        params = StdioServerParameters(command=sys.executable, args=[str(self.server_path)], env=dict(os.environ))
        r, w = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(r, w))
        await self.session.initialize()

    async def stop(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call(self, name: str, **args):
        async def _do():
            res = await self.session.call_tool(name, args)
            if not res.content:
                return getattr(res, "structured_content", None) or []
            text = res.content[0].text
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        self.calls += 1
        out, _retries = await with_retry(_do)
        # 리스트 반환 Tool은 FastMCP가 {"result": [...]}로 감싸는 경우가 있다
        if isinstance(out, dict) and set(out) == {"result"}:
            return out["result"]
        return out

    @traceable(run_type="tool", name="mcp.get_prompt")
    async def prompt(self, name: str, **args) -> str:
        res = await self.session.get_prompt(name, {k: str(v) for k, v in args.items()})
        return res.messages[0].content.text

    @traceable(run_type="tool", name="mcp.read_resource")
    async def resource(self, uri: str) -> str:
        res = await self.session.read_resource(uri)
        return res.contents[0].text
