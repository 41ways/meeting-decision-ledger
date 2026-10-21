"""용어(별칭) 색인 · 낱말 색인 · 좁힌 뒤 읽기 · 전체 읽기 대비책 · 원문 단어 검색 테스트 — LLM은 가짜, 원장은 시드 사본."""
# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument,redefined-outer-name,too-few-public-methods  # 가짜 객체·fixture 관용
import asyncio
from pathlib import Path

import pytest

from agent import ledger_graph as G
from agent import retrieve as R
from ledger import aliases as A
from ledger import term_index as T
from ledger.records import MeetingRecords
from ledger.store import DATA, LedgerStore


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 정규화 · 별칭 일치
def test_normalize_particles_case_and_spaces():
    assert A.strip_particle("캐시는") == "캐시" and A.strip_particle("레디스로") == "레디스"
    assert A.strip_particle("회의") == "회의" and A.strip_particle("평가") == "평가"     # 두 글자 낱말은 한 글자 조사를 떼지 않는다
    assert A.tokens("Redis캐시를 SSE로") == ["redis", "캐시를", "sse", "로"]
    assert A.content_words("Redis 캐시 쓰기로 했어요?") == ["redis", "캐시"]
    assert A.topics_of("long polling") == A.topics_of("Long Polling") == A.topics_of("LongPolling")


def test_redis_cache_goes_to_cache_not_session_store():
    """실제 사례: 'Redis 캐시'가 Redis를 기각한 대화 저장소로 새던 문제 — 긴 별칭이 이긴다."""
    assert A.topics_of("Redis 캐시 쓰기로 했어?") == [{"topic": "cache", "aliases": ["Redis 캐시"]}]
    assert A.topics_of("레디스 캐시에 넣어 두면")[0]["topic"] == "cache"
    assert A.topics_of("대화 저장은 레디스 어때요?")[0] == {"topic": "session.store", "aliases": ["대화 저장", "레디스"]}


def test_word_boundaries_and_glossary_variants():
    assert [h["topic"] for h in A.topics_of("불합격 처리")] == ["interview.decision"]
    assert A.match("불합격")[0].alias == "불합격"                      # '불합격' 속 '합격'은 따로 세지 않는다
    assert not A.topics_of("SSEs 쓰자")                                 # 영문 별칭은 낱말 끝까지
    hit = A.topics_of("그건 버니게이트로 코드에서 걸러요")[0]              # 용어집 STT 오인식도 별칭
    assert hit["topic"] == "agent.reliability" and "권위 게이트" in hit["aliases"][0]
    assert not A.topics_of("메신저를 팀즈로 바꾸기로 했나요?")


def test_choose_topic_alias_first_and_records_disagreement():
    ch = A.choose_topic("Redis 캐시 쓰기로 했어?", "session.store")
    assert ch["topic"] == "cache" and ch["by"] == "별칭" and ch["disagree"] and ch["aliases"] == ["Redis 캐시"]
    ch = A.choose_topic("응답 시간 기준을 첫 진행 표시 2초로", "perf.latency")
    assert ch["topic"] == "perf.latency" and not ch["disagree"]
    ch = A.choose_topic("SSE 응답 시간", "realtime.transport")       # 별칭이 여럿이면 그 안의 LLM 선택
    assert ch["topic"] == "realtime.transport" and ch["by"] == "별칭+LLM"
    assert A.choose_topic("회식 장소", "new_topic") == {"topic": "new_topic", "by": "LLM", "aliases": [],
                                                       "candidates": [], "llm": "new_topic", "disagree": False}


# ---------------------------------------------------------------- 낱말 색인 (원장 결정 · 미결)
@pytest.fixture
def db(tmp_path):
    s = LedgerStore(tmp_path / "ledger.db")
    s.load_seed()
    r = MeetingRecords(tmp_path / "ledger.db")
    r.sync_meetings()
    return s, r


def ids(out):
    return [c.get("decision_id") if c["kind"] == "decision" else c["item_id"] for c in out["candidates"]]


def test_index_finds_by_topic_with_reason_and_skips_superseded(db):
    s, r = db
    out = T.find_candidates(s, r, "PRJ-OPSAI", ["realtime.transport"], [], [], [])
    assert "OPS-0007" in ids(out) and "OPS-0003" not in ids(out)       # 대체된 결정은 후보에서 뺀다
    via = next(c["via"] for c in out["candidates"] if c.get("decision_id") == "OPS-0007")
    assert "realtime.transport(주제)" in via and out["tier"] == "주제·ID"
    assert ids(T.find_candidates(s, r, "PRJ-OPSAI", ["cache"], [], [], [])) == []     # 캐시 결정은 아직 없음


def test_index_id_crosses_project_and_meeting_scope(db):
    s, r = db
    assert ids(T.find_candidates(s, r, "PRJ-OPSAI", [], ["CSB-0004"], [], [])) == ["CSB-0004"]
    m02 = ids(T.find_candidates(s, r, "PRJ-OPSAI", [], [], ["OPS-M02"], []))
    assert {"OPS-0007", "OPS-0008"} <= set(m02) and "OPS-0001" not in m02
    both = ids(T.find_candidates(s, r, "PRJ-OPSAI", ["session.store"], [], ["OPS-M02"], []))
    assert both == ["OPS-0008"]                                          # 주제와 회의가 함께면 둘 다 맞는 것
    first = ids(T.find_candidates(s, r, "PRJ-OPSAI", ["realtime.transport"], ["OPS-0007"], [], []))
    assert first[0] == "OPS-0007" and "OPS-0005" in first                # 질문에 적힌 ID는 맨 앞


def test_index_word_tier_and_period(db):
    s, r = db
    out = T.find_candidates(s, r, "PRJ-OPSAI", [], [], [], ["postgresql"])
    assert out["tier"] == "단어" and ids(out) == ["OPS-0008"]
    out = T.find_candidates(s, r, "PRJ-OPSAI", ["realtime.transport"], [], [], [],
                            ("2026-09-01", "2026-09-20"))
    assert out["period_dropped"] and ids(out)        # 기간 안에 없으면 기간 조건을 빼고 알린다


def test_index_follows_ledger_and_record_writes(db):
    s, r = db
    did = s.propose({"project_id": "PRJ-OPSAI", "topic": "budget", "statement": "런북 캐시는 문서만 적용",
                     "decided_by": "서지후", "evidence": [{"meeting_id": "OPS-M04", "seq": 11, "quote": "q"}]})
    assert did in ids(T.find_candidates(s, r, "PRJ-OPSAI", ["cache"], [], [], []))   # 문장 속 별칭으로도 걸린다
    s.update_proposed(did, topic="cache")
    via = T.find_candidates(s, r, "PRJ-OPSAI", ["cache"], [], [], [])["candidates"][0]["via"]
    assert "cache(주제)" in via
    r.save_meeting("OPS-M03", None, [{"kind": "open", "seq": 10, "statement": "캐시 대상 정의", "topic": "cache",
                                      "state": "open",
                                      "evidence": [{"meeting_id": "OPS-M03", "seq": 10, "quote": "x"}]}],
                   "test")
    assert "OPS-M03:open:10" in ids(T.find_candidates(s, r, "PRJ-OPSAI", ["cache"], [], [], []))
    r.close_open("OPS-M03:open:10", "pdy", "회의 밖에서 정함")
    assert "OPS-M03:open:10" not in ids(T.find_candidates(s, r, "PRJ-OPSAI", ["cache"], [], [], []))


def test_index_rebuilds_when_alias_version_changes(db, monkeypatch):
    s, _ = db
    import sqlite3   # pylint: disable=import-outside-toplevel
    with sqlite3.connect(s.db_path) as c:
        c.execute("DELETE FROM term_index")
        c.execute("UPDATE index_meta SET value='old' WHERE name='ver:D'")
    LedgerStore(s.db_path)             # 다시 열면 판이 달라 결정 색인을 다시 만든다
    with sqlite3.connect(s.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM term_index WHERE kind='D'").fetchone()[0] > 0


# ---------------------------------------------------------------- 좁힌 뒤 읽기 · 전체 읽기 대비책
def cand(n, topic="cache"):
    return {"kind": "decision", "decision_id": f"OPS-{n:04d}", "project_id": "PRJ-OPSAI", "topic": topic,
            "status": "approved", "active": True, "statement": f"결정 {n}"}


class PickMCP:
    def __init__(self, found, everything=None):
        self.found, self.everything, self.calls = found, everything or [], []

    async def call(self, name, **a):
        self.calls.append((name, a))
        if name == "index_candidates":
            return {"candidates": self.found, "tier": "주제·ID" if self.found else "", "period_dropped": False}
        if name == "project_records_all":
            return self.everything
        raise KeyError(name)


class PickLLM:
    def __init__(self, ids=()):
        self.ids, self.prompts = list(ids), []

    async def json(self, system, user, schema, **kw):
        self.prompts.append(user)
        return {"ids": self.ids}, {"tokens": 10}


def deps(mcp, llm):
    return type("D", (), {"mcp": mcp, "quality_llm": llm})()


def test_pick_reads_only_narrowed_list():
    mcp, llm = PickMCP([cand(8), cand(7)]), PickLLM(["D:OPS-0008", "D:OPS-9999"])
    out = run(R.pick(deps(mcp, llm), "Redis 캐시 쓰기로 했어?", "PRJ-OPSAI"))
    assert out["how"] == "색인" and out["shown"] == 2 and [d["decision_id"] for d in out["decisions"]] == ["OPS-0008"]
    assert mcp.calls[0][1]["topics"] == ["cache"] and not out["full_read"]
    assert [n for n, _ in mcp.calls] == ["index_candidates"]          # 전체 목록은 읽지 않는다


def test_candidate_limit_triggers_chunked_full_read_with_notice():
    many = [cand(n) for n in range(1, 52)]                               # 51건 > 50건 상한
    allrec = [cand(n) for n in range(1, 400)]
    mcp, llm = PickMCP(many, allrec), PickLLM(["D:OPS-0003"])
    out = run(R.pick(deps(mcp, llm), "캐시 어떻게 했어?", "PRJ-OPSAI", project_key="OPS"))
    fr = out["full_read"]
    assert out["how"] == "전체" and fr["reason"] == "충돌" and fr["read"] == 399 and fr["parts"] == len(llm.prompts) > 1
    assert fr["notice"].startswith("※ 색인 후보가 51건으로 많아(별칭 충돌) OPS 기록 전체 399건을 읽었습니다 · 약 ")
    assert [d["decision_id"] for d in out["decisions"]] == ["OPS-0003"]   # 나눠 읽은 결과를 중복 없이 합친다


def test_full_read_conditions():
    assert R.full_read_reason("캐시 전부 찾아봐", 3, True) == "요청"
    assert R.full_read_reason("캐시?", 51, True) == "충돌" and R.full_read_reason("캐시?", 50, True) == ""
    assert R.full_read_reason("캐시?", 0, True) == "색인 0건" and R.full_read_reason("회식?", 0, False) == ""
    # 알려진 주제(LLM 선택)인데 색인 0건 → 전체 읽기
    mcp, llm = PickMCP([], [cand(1), cand(2)]), PickLLM([])
    out = run(R.pick(deps(mcp, llm), "그거 어떻게 됐어?", "PRJ-OPSAI", topic="schedule", project_key="OPS"))
    assert out["full_read"]["notice"].startswith("※ 색인에서 찾지 못해 OPS 기록 전체 2건을 읽었습니다 · 약 ")
    # 아무 단서도 없으면 전체 읽기도 LLM 호출도 하지 않는다
    mcp, llm = PickMCP([], [cand(1)]), PickLLM([])
    out = run(R.pick(deps(mcp, llm), "회식 장소 정했어?", "PRJ-OPSAI"))
    assert out["how"] == "없음" and not llm.prompts and [n for n, _ in mcp.calls] == ["index_candidates"]


def test_notice_text_and_chunking():
    assert R.full_read_notice("색인 0건", "OPS", 15, 0, 4.4, 1) == "※ 색인에서 찾지 못해 OPS 기록 전체 15건을 읽었습니다 · 약 4초"
    assert R.full_read_notice("요청", "CSB", 300, 2, 31, 3).endswith("약 31초 · 3번에 나눠 읽음")
    parts = R.chunks(["가" * 160] * 10, budget=300)                   # 한 줄 약 100토큰 → 두 줄씩
    assert [len(p) for p in parts] == [2] * 5


def test_finalize_puts_notice_before_answer():
    st = {"mode": "question", "draft": "[확정] OPS-0008입니다.", "notice": "※ 요청에 따라 OPS 기록 전체 9건을 읽었습니다 · 약 3초",
          "items": [], "events": []}
    out = run(G.finalize_node(st, deps=type("D", (), {"propose_to_ledger": False})()))
    assert out["final"].startswith("※ 요청에 따라 OPS 기록 전체 9건") and out["final"].endswith("[확정] OPS-0008입니다.")


# ---------------------------------------------------------------- 회의 원문 단어 검색
@pytest.fixture
def tx(tmp_path):
    t = T.TranscriptIndex(tmp_path / "ledger.db")
    t.sync(DATA)
    return t


def test_transcript_word_search(tx):
    rows = tx.search("Redis 캐시 쓰기로 했어요?", project_id="PRJ-OPSAI", k=10)
    assert {r["meeting_id"] for r in rows} <= {"OPS-M03", "OPS-M04"}
    assert ("OPS-M03", 10) in {(r["meeting_id"], r["seq"]) for r in rows}
    assert all("#cache" in r["matched"] for r in rows)
    assert not tx.search("메신저를 팀즈로 바꾸기로 했나요?")         # 논의한 적 없는 주제엔 아무 발언도 붙지 않는다
    one = tx.search("마스킹 스크립트 누가 만들어?")
    assert (one[0]["meeting_id"], one[0]["seq"]) == ("OPS-M05", 12)
    assert all(r["meeting_id"] == "OPS-M04" for r in tx.search("캐시", meeting_id="OPS-M04"))


def test_transcript_index_drops_deleted_meeting(tx, tmp_path):
    assert tx.delete("OPS-M03") > 0 and all(r["meeting_id"] != "OPS-M03" for r in tx.search("캐시", k=20))
    assert tx.sync(DATA) == 1                                           # 파일이 남아 있으면 다시 색인 (바뀐 것만)
    assert tx.sync(DATA) == 0
    assert Path(DATA / "r1_meetings" / "OPS-M03.json").exists()
