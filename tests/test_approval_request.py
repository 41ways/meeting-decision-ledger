"""의견 검토 답 → 승인 요청 테스트 — 받는 사람·요청 내용·사유가 승인 대기로만 올라가고 알림이 가는지, 화면 초안 생성.

MCP는 가짜, 권한 저장소는 임시 SQLite. 원장 직접 쓰기(approve)는 일어나면 안 된다.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring,redefined-outer-name,unused-argument  # pytest fixture·가짜 객체 관용
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import server
from ledger.access import AccessStore

ROOT = Path(__file__).resolve().parent.parent
DEMO4 = ("**검토 결과: 충돌** (주제 session.store)\n- 기존 결정과 다름 [OPS-0008] PostgreSQL 사용 필수\n"
         "- 다른 프로젝트 참고 [CSB-0005] Redis 사용 금지\n- 참고: 다른 프로젝트 결정은 참고용: CSB-0005\n▶ 해결 방향\n"
         "  · 현재 유효 결정 OPS-0008를 바꾸는 결정 — 팀장이 승인하면 OPS-0008가 대체됨. OPS-0008에서 기각했던 대안: Redis")


class FakeMcp:
    def __init__(self):
        self.calls = []

    async def call(self, name, **kw):
        self.calls.append((name, kw))
        row = {"decision_id": "OPS-0099", "status": "proposed", "statement": "세션 저장소를 Redis로 변경",
               "topic": "session.store", "decided_by": "서지후", "evidence": []}
        return {"propose_decision": {"proposed": "OPS-0099"}, "list_decisions": [row],
                "get_decision": row | {"rationale": "OPS-0008과 충돌"}, "decision_history": {"items": []},
                "will_supersede": [{"decision_id": "OPS-0008"}]}.get(name, {})


@pytest.fixture
def state(tmp_path, monkeypatch):
    mcp = FakeMcp()
    monkeypatch.setattr(server.app, "state", SimpleNamespace(access=AccessStore(tmp_path / "access.db"),
                                                             deps=SimpleNamespace(mcp=mcp)))
    monkeypatch.setattr(server.monitor_api, "link_decision", lambda *a: None)
    return server.app.state


def test_request_goes_to_pending_with_edited_text_and_reason(state):
    req = server.ProposalReq(user="pdy", statement="세션 저장소를 Redis로 변경 (실험 환경 한정)", topic="session.store",
                             reason="기존 결정 OPS-0008과 충돌. 승인하면 OPS-0008이 대체됨", to="kjh")
    r = asyncio.run(server.propose(req))
    assert r["proposed"] == "OPS-0099"
    names = [c[0] for c in state.deps.mcp.calls]
    assert names == ["propose_decision"]                    # 승인 대기로만 — 승인·원장 직접 쓰기 없음
    kw = state.deps.mcp.calls[0][1]
    assert kw["statement"] == "세션 저장소를 Redis로 변경 (실험 환경 한정)" and kw["rationale"].startswith("기존 결정 OPS-0008")
    assert kw["evidence"] == [] and kw["decided_by"] == "서지후"
    assert state.access.proposal_sources()["OPS-0099"]["requested_by"] == "pdy"
    assert any("OPS-0099 승인 요청" in n["text"] for n in state.access.notifications("kjh"))


def test_lead_console_shows_pending_with_reason(state):
    asyncio.run(server.propose(server.ProposalReq(user="pdy", statement="세션 저장소를 Redis로 변경", topic="session.store",
                                                  reason="OPS-0008과 충돌")))
    out = asyncio.run(server.admin_pending("kjh"))
    d = out["decisions"][0]
    assert (d["decision_id"], d["requested_by"], d["rationale"]) == ("OPS-0099", "서지후", "OPS-0008과 충돌")
    with pytest.raises(HTTPException):
        asyncio.run(server.admin_pending("pdy"))             # 팀장 전용


def test_non_lead_recipient_also_notifies_lead(state):
    asyncio.run(server.propose(server.ProposalReq(user="yhr", statement="X로 변경", topic="t", to="pdy")))
    assert state.access.notifications("pdy") and state.access.notifications("kjh")


def test_blank_statement_and_unknown_recipient_rejected(state):
    with pytest.raises(HTTPException):
        asyncio.run(server.propose(server.ProposalReq(user="pdy", statement="  ", topic="t")))
    with pytest.raises(HTTPException):
        asyncio.run(server.propose(server.ProposalReq(user="pdy", statement="X", topic="t", to="nobody")))
    assert not state.deps.mcp.calls


DRAFT = r"""
const fs = require('fs'); const h = fs.readFileSync(process.argv[1], 'utf8');
const a = h.indexOf('    function toDecision'), b = h.indexOf('    const ap = reactive');
const users = { value: JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).users };
eval(h.slice(a, b));
console.log(JSON.stringify([approvalDraft('세션 저장소를 Redis로 바꾸면 어때?', process.argv[3]),
                            approvalDraft('SSE는 왜 골랐어?', '**답변**\nSSE를 골랐습니다 [OPS-0003]')]));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_screen_prefills_from_saved_answer():
    out = subprocess.run(["node", "-e", DRAFT, str(ROOT / "web_chat.html"), str(ROOT / "data/project/users.json"), DEMO4],
                         capture_output=True, text=True, check=True).stdout
    d, none = json.loads(out)
    assert none is None                                     # 질의 답에는 버튼 없음
    assert d["to"] == "kjh" and d["statement"] == "세션 저장소를 Redis로 변경" and d["topic"] == "session.store"
    assert d["reason"] == "기존 결정 OPS-0008(PostgreSQL 사용 필수)과 충돌. 팀장이 승인하면 OPS-0008가 대체됨"
