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
    """
    결정 원장 MCP 서버와 stdio로 연결하는 클라이언트 (Tool·Prompt·Resource 호출)

    Attributes:
        server_path:  실행할 MCP 서버 스크립트 경로
        session:  start() 이후 열린 MCP 세션. 시작 전에는 None
        calls:  call()로 Tool을 호출한 횟수 (재시도는 세지 않음)
    """

    def __init__(self, server_path: Path = ROOT / "mcp_ledger_server.py"):
        """
        클라이언트 상태 초기화. 실제 연결은 start()에서
        Args:
            server_path:  MCP 서버 스크립트 경로
        """
        self.server_path = server_path
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.calls = 0

    async def start(self) -> None:
        """
        MCP 서버를 자식 프로세스로 띄우고 세션을 초기화

        stdio 연결과 세션을 AsyncExitStack에 묶어 stop() 한 번으로 함께 정리.
        """
        self._stack = AsyncExitStack()
        # 환경변수를 그대로 넘긴다 (stdio 기본값은 PATH 등 일부만 전달 — 평가용 LEDGER_STORE, OLLAMA_URL이 빠짐)
        params = StdioServerParameters(command=sys.executable, args=[str(self.server_path)], env=dict(os.environ))
        r, w = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(r, w))
        await self.session.initialize()

    async def stop(self) -> None:
        """
        세션과 자식 프로세스 종료. 시작하지 않았거나 이미 멈췄으면 아무것도 하지 않음
        """
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call(self, name: str, **args):
        """
        MCP Tool 호출. 타임아웃이면 with_retry로 재시도
        Args:
            name:  Tool 이름
            **args:  Tool 인자
        Returns:
            JSON으로 파싱된 결과(dict·list). JSON이 아니면 원문 문자열,
            content가 비면 structured_content 또는 빈 목록
        """
        async def _do():
            """
            Tool을 한 번 호출하고 첫 content를 해석
            Returns:
                파싱된 JSON 값, 파싱 실패 시 원문 문자열, content가 없으면 structured_content 또는 []
            """
            res = await self.session.call_tool(name, args)
            # content가 빈 응답이면 res.content[0]에서 IndexError가 나므로 먼저 거름
            if not res.content:
                return getattr(res, "structured_content", None) or []
            text = res.content[0].text
            # Tool이 문자열을 그대로 돌려주는 경우도 있어 JSON 실패는 오류로 보지 않음
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
        """
        MCP 서버에 등록된 프롬프트 템플릿을 채워 받아옴

        MCP 프롬프트 인자는 문자열만 받으므로 값을 str로 변환해 전달.
        Args:
            name:  프롬프트 이름
            **args:  템플릿 인자
        Returns:
            첫 메시지의 본문 텍스트
        """
        res = await self.session.get_prompt(name, {k: str(v) for k, v in args.items()})
        return res.messages[0].content.text

    @traceable(run_type="tool", name="mcp.read_resource")
    async def resource(self, uri: str) -> str:
        """
        MCP Resource 읽기
        Args:
            uri:  리소스 URI
        Returns:
            첫 콘텐츠의 텍스트
        """
        res = await self.session.read_resource(uri)
        return res.contents[0].text
