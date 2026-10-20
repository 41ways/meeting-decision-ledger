"""MCP 서버(decision-ledger)를 stdio로 띄워 Tool·Resource·Prompt가 노출되고 동작하는지 확인한다."""
import asyncio, json, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent

async def main():
    """
    MCP 서버를 stdio 자식 프로세스로 띄워 노출 목록과 대표 Tool·Resource·Prompt 호출 결과를 출력

    대표 사례: 현재 결정 해석(resolve_current), 결정 권한(check_authority), 정책 예외(check_waiver),
    원문 발언 조회(get_segments, 원문이 파기된 CS-M09 포함), 현행 정책 Resource, 충돌 검토 Prompt.
    """
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_ledger_server.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("Tools:", [t.name for t in (await s.list_tools()).tools])
            # MCP SDK 버전에 따라 필드 이름이 snake_case/camelCase로 달라 둘 다 받는다
            rt = await s.list_resource_templates()
            items = getattr(rt, "resource_templates", None) or getattr(rt, "resourceTemplates", [])
            print("Resource templates:", [getattr(t, "uri_template", None) or getattr(t, "uriTemplate", None) for t in items])
            print("Prompts:", [p.name for p in (await s.list_prompts()).prompts])
            async def call(name, args):
                """
                Tool 하나를 호출하고 텍스트 응답을 JSON이면 파싱
                Args:
                    name: Tool 이름
                    args: Tool 인자
                Returns:
                    JSON 객체·배열이면 파싱 결과, 아니면 원문 텍스트
                """
                res = await s.call_tool(name, args)
                txt = res.content[0].text if res.content else ""
                return json.loads(txt) if txt.startswith(("{","[")) else txt
            r1 = await call("resolve_current", {"text": "대화 저장 DB를 MySQL로 바꾸죠"})
            print("resolve_current:", r1["topic"], r1["authority"], r1["current"]["decision_id"], [x["rule_id"] for x in r1["rules"]], r1["notes"])
            print("check_authority:", await call("check_authority", {"topic": "schedule", "decided_by_role": "PL", "attendee_roles": ["PL","백엔드"]}))
            print("check_waiver:", (await call("check_waiver", {"rule_id": "ARC-02", "on_date": "2026-10-13"}))["note"])
            seg = await call("get_segments", {"meeting_id": "M04", "seq_from": 3, "seq_to": 4})
            print("get_segments:", [(x["seq"], x["speaker"], x["text"][:25]) for x in seg])
            print("get_segments(CS-M09 파기):", await call("get_segments", {"meeting_id": "CS-M09"}))
            rr = await s.read_resource("policy://current/SEC-01")
            print("resource policy://current/SEC-01:", rr.contents[0].text[:90])
            pr = await s.get_prompt("conflict_review_prompt", {"statement": "실시간 전송을 웹소켓으로 변경"})
            print("prompt:", pr.messages[0].content.text.splitlines()[0])

asyncio.run(main())
