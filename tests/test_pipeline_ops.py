"""파이프라인 감시·버전 관리 테스트 — 버전 지문, 요청 지표, 드리프트 판정, 승격·거부·롤백 (docs/pipeline_ops.md)."""
# pylint: disable=missing-function-docstring,missing-class-docstring,redefined-outer-name,unused-argument  # pytest fixture·가짜 객체 관용
import asyncio
import json
import random
import shutil
from pathlib import Path

import pytest

from agent import ledger_graph as G
from ledger import monitor
from ledger import pipeline_registry as PR
from ledger.metrics import MetricsStore, check_kind, extract, human_rates, human_signals, oov_ratio, top1_score
from ledger.records import MeetingRecords
from ledger.store import LedgerStore
from scripts import promote
from tests.test_graph import FakeLLM, FakeMCP

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path):
    """버전 구성 파일만 복사한 가짜 저장소"""
    for rel in {*PR.CODE_FILES, *PR.PROMPT_FILES, PR.THRESHOLD_FILE, *PR.VOCAB_FILES}:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    return tmp_path


def _vid(root):
    return PR.version_id(PR.components("ollama:qwen3:8b", "ollama:qwen3:8b", root))


# ---------------------------------------------------------------- 버전 지문
def test_version_is_stable_and_ignores_comments_and_docstrings(repo):
    v0 = _vid(repo)
    assert v0 == _vid(repo) and v0.startswith("pv-")
    f = repo / "agent/retrieve.py"
    f.write_text(f.read_text(encoding="utf-8").replace('"""근거 질문의 후보 선택', '"""(설명만 고침) 근거 질문의 후보 선택')
                 + "\n# 주석만 추가\n", encoding="utf-8")
    assert _vid(repo) == v0


def test_version_changes_with_threshold_prompt_vocab_model(repo):
    v0 = _vid(repo)
    th = repo / PR.THRESHOLD_FILE
    th.write_text(th.read_text(encoding="utf-8").replace("MIN_TOPIC_SCORE = 0.50", "MIN_TOPIC_SCORE = 0.52"),
                  encoding="utf-8")
    v1 = _vid(repo)
    assert v1 != v0
    c1 = PR.components("ollama:qwen3:8b", "ollama:qwen3:8b", repo)
    f = repo / "agent/retrieve.py"
    f.write_text(f.read_text(encoding="utf-8").replace("질문에 답하는 데 직접 필요한", "질문에 꼭 필요한"), encoding="utf-8")
    c2 = PR.components("ollama:qwen3:8b", "ollama:qwen3:8b", repo)
    assert c2["prompt_fp"] != c1["prompt_fp"] and PR.diff_components(c1, c2)["prompts"] == ["agent/retrieve.py:read_list#1"]
    t = repo / "data/project/topics.json"
    t.write_text(t.read_text(encoding="utf-8").replace('"routing"', '"routing2"'), encoding="utf-8")
    assert PR.components("ollama:qwen3:8b", "ollama:qwen3:8b", repo)["vocab_fp"] != c2["vocab_fp"]
    assert PR.version_id(PR.components("ollama:qwen3:0.6b", "ollama:qwen3:8b", repo)) != _vid(repo)


def test_crosscheck_switch_is_part_of_version(monkeypatch):
    off = PR.version_id(PR.components("ollama:qwen3:8b", "ollama:qwen3:8b"))
    monkeypatch.setenv("CROSSCHECK", "1")
    comp = PR.components("ollama:qwen3:8b", "ollama:qwen3:8b")
    assert comp["models"]["checkers"] == ["ollama:qwen3:0.6b", "ollama:qwen2.5:0.5b"] and PR.version_id(comp) != off

    class D:
        fast_llm = quality_llm = type("L", (), {"name": "ollama:qwen3:8b"})()
        checkers = [type("C", (), {"name": "ollama:qwen3:0.6b"})(), type("C", (), {"name": "ollama:qwen2.5:0.5b"})()]
    assert PR.version_of(D()) == PR.version_id(comp)        # 그래프 의존성과 환경변수가 같은 버전을 낸다


def test_prompts_cover_inline_and_named_system_prompts():
    keys = PR.prompt_texts()
    assert "agent/retrieve.py:read_list#1" in keys                     # 상수(PICK_SYSTEM)로 넘긴 지시문
    assert any(k.startswith("agent/ledger_graph.py:plan_node#") for k in keys)   # 문장을 바로 쓴 지시문
    assert "mcp_ledger_server.py:extract_decisions_prompt" in keys
    assert "질문에 답하는 데" in keys["agent/retrieve.py:read_list#1"]


# ---------------------------------------------------------------- 레지스트리·승격·롤백
def _comp(th):
    c = PR.components("ollama:qwen3:8b", "ollama:qwen3:8b")
    return {**c, "thresholds": {**c["thresholds"], "MIN_TOPIC_SCORE": th}}


def test_registry_register_promote_rollback(tmp_path):
    reg = PR.Registry(tmp_path / "r.json")
    a, b = reg.register(_comp(0.50)), reg.register(_comp(0.60))
    assert a["status"] == b["status"] == "challenger" and reg.register(_comp(0.50)) is a
    reg.promote(a["id"])
    out = reg.promote(b["id"])
    assert out["retired"] == a["id"] and reg.get(a["id"])["status"] == "retired"
    reg.save()
    reg = PR.Registry(tmp_path / "r.json")
    assert reg.champion()["id"] == b["id"]
    back = reg.rollback()
    assert back == {"champion": a["id"], "retired": b["id"]}
    assert reg.get(b["id"])["status"] == "retired" and reg.champion()["id"] == a["id"]
    with pytest.raises(ValueError):
        reg.rollback()                                   # 더 앞 champion이 없음


CHAMP = {"결정 재현율(%)": 100, "충돌 오탐(건)": 0, "평균 처리 시간(초)": 20.0, "토큰": 40000}


def test_judge_promotion_rejects_any_drop_and_warns_on_time():
    j = promote.judge_promotion(CHAMP, {**CHAMP, "결정 재현율(%)": 91})
    assert j["verdict"] == "reject" and "결정 재현율(%)" in j["drops"][0]
    assert promote.judge_promotion(CHAMP, {**CHAMP, "충돌 오탐(건)": 1})["verdict"] == "reject"
    j = promote.judge_promotion(CHAMP, {**CHAMP, "평균 처리 시간(초)": 27.0, "토큰": 90000})
    assert j["verdict"] == "pass" and len(j["warnings"]) == 1            # 시간은 경고만, 토큰은 참고
    assert promote.judge_promotion(CHAMP, {**CHAMP, "평균 처리 시간(초)": 25.0})["warnings"] == []


def test_promote_script_pass_then_reject_then_rollback(tmp_path, monkeypatch):
    regf = tmp_path / "reg.json"
    # 지금 버전을 champion으로 두고, 임계값을 바꾼 challenger를 결과 파일로 평가한다
    reg = PR.Registry(regf)
    champ = reg.register(_comp(0.60))
    reg.promote(champ["id"])
    reg.add_eval(champ["id"], {"runner": "after", "scores": CHAMP, "verdict": "pass"})
    reg.save()
    worse, same = tmp_path / "worse.json", tmp_path / "same.json"
    worse.write_text(json.dumps({"scores": {"After 결정 원장": {**CHAMP, "결정 재현율(%)": 90}}}), encoding="utf-8")
    same.write_text(json.dumps({"scores": {"After 결정 원장": CHAMP}}), encoding="utf-8")
    args = ["--registry", str(regf), "--from-results"]
    assert promote.main(args + [str(worse)]) == 1                         # 하락 → 거부
    reg = PR.Registry(regf)
    cur = PR.version_id(PR.components("ollama:qwen3:8b", "ollama:qwen3:8b"))
    assert reg.champion()["id"] == champ["id"] and reg.get(cur)["status"] == "challenger"
    assert reg.data["log"][-1]["action"] == "reject"
    assert promote.main(args + [str(same)]) == 0                          # 유지 → 승격
    reg = PR.Registry(regf)
    assert reg.champion()["id"] == cur and reg.get(champ["id"])["status"] == "retired"
    # 롤백: 이전 champion으로 돌리고, 임계값 차이를 안내한다 (--apply 없이 파일은 그대로)
    th = (ROOT / PR.THRESHOLD_FILE).read_text(encoding="utf-8")
    assert promote.main(["--registry", str(regf), "--rollback"]) == 0
    assert PR.Registry(regf).champion()["id"] == champ["id"]
    assert (ROOT / PR.THRESHOLD_FILE).read_text(encoding="utf-8") == th


def test_eval_crash_is_recorded_not_promoted(tmp_path):
    regf = tmp_path / "reg.json"
    args = ["--registry", str(regf), "--allow-busy", "--eval-cmd", f"{promote.PY} -c raise(SystemExit(3))"]
    assert promote.main(args) == 1
    reg = PR.Registry(regf)
    cur = PR.version_id(PR.components("ollama:qwen3:8b", "ollama:qwen3:8b"))
    assert reg.get(cur)["evals"][-1]["verdict"] == "eval_failed" and reg.champion() is None


def test_apply_thresholds_rewrites_constants(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    shutil.copy(ROOT / PR.THRESHOLD_FILE, tmp_path / PR.THRESHOLD_FILE)
    monkeypatch.setattr(promote, "ROOT", tmp_path)
    assert promote.apply_thresholds({"MIN_TOPIC_SCORE": 0.6}) == ["MIN_TOPIC_SCORE = 0.6"]
    assert PR.thresholds(tmp_path)["MIN_TOPIC_SCORE"] == 0.6
    assert "MIN_TOPIC_SCORE = 0.60" in (tmp_path / PR.THRESHOLD_FILE).read_text(encoding="utf-8")   # 자릿수 유지
    promote.apply_thresholds({"MIN_TOPIC_SCORE": 0.5})
    assert (tmp_path / PR.THRESHOLD_FILE).read_text(encoding="utf-8") == (ROOT / PR.THRESHOLD_FILE).read_text(encoding="utf-8")


def test_read_scores_accepts_run_eval_and_flat_formats(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"runs": {}, "scores": {"After 결정 원장": CHAMP, "Before-A": {"x(%)": 1}}}))
    b.write_text(json.dumps({"dev_acc": 0.9, "note": "문자열은 뺀다"}))
    assert promote.read_scores(a, "After 결정 원장") == CHAMP
    assert promote.read_scores(b) == {"dev_acc": 0.9}


# ---------------------------------------------------------------- 요청 지표
def run(coro):
    return asyncio.run(coro)


class BrokenJudgeLLM(FakeLLM):
    async def json(self, system, user, schema, **kw):
        if "checks" in schema["properties"]:
            return {}, {"tokens": 1, "parse_error": True}
        return await super().json(system, user, schema, **kw)


def test_extract_meeting_metrics_from_graph_state(tmp_path):
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=BrokenJudgeLLM(), mcp=FakeMCP()))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    row = extract(st, 12.5, "pv-test", "process", "agent")
    assert row["mode"] == "meeting" and row["parse_errors"] == 1 and row["llm_calls"] == st["usage"]["llm_calls"]
    assert row["labels"] == {"확인 필요": 1} and row["check_kinds"] == {"판정 실패": 1}
    assert row["items"] == 1 and row["no_topic"] == 0 and row["oov_ratio"] is not None
    ms = MetricsStore(tmp_path / "m.db")
    ms.record(row)
    got = ms.rows(version="pv-test")
    assert len(got) == 1 and got[0]["labels"] == {"확인 필요": 1}


def test_parse_errors_do_not_change_pipeline_result():
    """기록용으로 usage에 parse_errors를 더해도 판정·답은 그대로다."""
    graph = G.build_ledger_graph(G.LedgerDeps(fast_llm=FakeLLM(), quality_llm=FakeLLM(), mcp=FakeMCP()))
    st = run(graph.ainvoke({"message": "OPS-M04 회의 정리해줘", "events": []}))
    assert st["usage"].get("parse_errors", 0) == 0 and st["items"][0]["review"]["changes"] == ["OPS-0007"]


def test_extract_question_label_rewrites_judge_and_fallback_event():
    st = {"mode": "question", "message": "캐시는 어떻게 하기로 했어?", "usage": {"llm_calls": 4, "tokens": 9},
          "items": [{"topic": "", "research": {"authority": "none", "topic_support": 0.41, "pending": [],
                                               "rules": [{"_score": 0.47}]}}],
          "events": [{"type": "FullReadFallback"},
                     {"type": "Validation", "verdict": "불합격", "judge": {"scores": {"relevance": 2, "clarity": 4}}},
                     {"type": "Validation", "verdict": "합격", "judge": {"scores": {"relevance": 4, "clarity": 5}}}]}
    row = extract(st, 20.0, "pv-x")
    assert row["labels"] == {"찾지 못함": 1} and row["rewrites"] == 1 and row["judge_min"] == 4
    assert row["full_read"] == 1 and row["no_topic"] == 1 and row["below_floor"] == 1 and row["top1"] == [0.47]


def test_node_latency_recorded_and_old_db_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:                         # node_ms 칸이 없던 옛 표
        c.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, version TEXT, "
                  "source TEXT, mode TEXT, user TEXT, model TEXT, cached INTEGER NOT NULL DEFAULT 0, error TEXT, "
                  "seconds REAL, llm_calls INTEGER, tokens INTEGER, parse_errors INTEGER, rewrites INTEGER, "
                  "judge_min INTEGER, items INTEGER, no_topic INTEGER, below_floor INTEGER, full_read INTEGER, "
                  "oov_ratio REAL, labels TEXT, check_kinds TEXT, top1 TEXT, proposed TEXT)")
    ms = MetricsStore(db)
    for w in (900, 1500, 3000):
        ms.record(extract({"mode": "question", "node_ms": {"write": w, "route": 10.4}}, 1.0, "pv-n"))
    rows = ms.rows()
    assert rows[0]["node_ms"] == {"write": 900, "route": 10}
    assert monitor.node_latency(rows)[0] == {"node": "write", "n": 3, "median_ms": 1500, "max_ms": 3000}


def test_run_config_tags_langsmith_metadata():
    from types import SimpleNamespace
    from ledger import monitor_api
    st = SimpleNamespace(versions={"accurate": "pv-a", "fast": "pv-f"})
    assert monitor_api.run_config(st, "fast")["metadata"] == {"pipeline_version": "pv-f", "model_preset": "fast"}
    assert monitor_api.run_config(st, "모르는 키")["metadata"]["pipeline_version"] == "pv-a"


def test_check_kind_and_helpers():
    assert check_kind("규정 충돌 · 권한 초과") == "규정 충돌"
    assert check_kind("주제 불확실 — 결정·기준 비교 결과를 확인해야 함") == "주제 불확실"
    assert check_kind("뭔가 새로운 사유") == "기타"
    assert top1_score({"topic_support": 0.6, "similar_other": [{"score": 0.7}]}) == 0.7
    assert top1_score({}) is None
    vocab = {"캐시", "세션", "redis"}
    assert oov_ratio("캐시를 Redis로 바꾸면 어때", vocab) == 0.0
    assert oov_ratio("양자컴퓨터 도입", vocab) == 1.0


def test_human_signals_map_to_version(tmp_path):
    rec = MeetingRecords(tmp_path / "l.db")
    rec.log("서지후", "decision_approved", "OPS-0012")
    rec.log("서지후", "decision_rejected", "OPS-0013")
    rec.log("서지후", "decision_edited", "OPS-0012")
    rec.log("서지후", "item_closed", "OPS-M04:open:9")
    rec.log("agent", "meeting_processed", "OPS-M04")                   # 사람 신호 아님
    meetings = [{"meeting_id": "OPS-M04", "pipeline_version": "pv-old"}]
    sig = human_signals(rec.events(), meetings, lambda d: [], {"OPS-0012": "pv-new"})
    assert [(s["kind"], s["version"]) for s in sig] == [("approved", "pv-new"), ("rejected", "미상"),
                                                        ("edited", "pv-new"), ("closed", "pv-old")]
    r = human_rates(sig)
    assert r["decided"] == 2 and r["reject_rate"] == 0.5 and r["edit_rate"] == 0.5


# ---------------------------------------------------------------- 드리프트 판정 (가짜 분포)
def _rows(n, seed, score_mu=0.62, check_p=0.2, sec=20.0, parse_p=0.0, no_topic_p=0.1):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        lab = "확인 필요" if rnd.random() < check_p else rnd.choice(["확정", "승인 대기", "미결"])
        out.append({"at": f"2026-10-21T10:{i:02d}:00", "mode": "question", "error": None,
                    "top1": [round(min(max(rnd.gauss(score_mu, 0.04), 0), 1), 3)], "items": 1,
                    "no_topic": int(rnd.random() < no_topic_p), "below_floor": 0, "oov_ratio": 0.2,
                    "labels": {lab: 1}, "llm_calls": 5, "parse_errors": sum(rnd.random() < parse_p for _ in range(5)),
                    "rewrites": 0, "judge_min": 4, "full_read": 0, "seconds": rnd.gauss(sec, 1.0)})
    return out


def _levels(findings):
    return {f["metric"]: f["level"] for f in findings}


def test_drift_stable_windows_are_normal():
    lv = _levels(monitor.drift_findings(_rows(60, 1), _rows(60, 2)))
    assert lv["검색 1위 점수 분포"] == "정상" and lv["확인 필요 비율"] == "정상"
    assert lv["응답 시간 중앙값(question)"] == "정상" and lv["JSON 파싱 실패율(호출당)"] == "정상"
    assert lv["반려율"] == "부족"                                         # 사람 신호가 없으면 판정하지 않는다


def test_drift_detects_shifted_distributions():
    base = _rows(60, 1)
    shifted = _rows(60, 3, score_mu=0.50, check_p=0.6, sec=30.0, parse_p=0.15, no_topic_p=0.5)
    lv = _levels(monitor.drift_findings(base, shifted))
    assert lv["검색 1위 점수 분포"] == "경보" and lv["확인 필요 비율"] == "경보"
    assert lv["응답 시간 중앙값(question)"] == "경보" and lv["JSON 파싱 실패율(호출당)"] == "경보"
    assert lv["새 주제(주제 없음) 비율"] == "경보" and lv["라벨 분포"] == "경보"


def test_drift_small_sample_is_not_judged_and_human_signal_alerts():
    lv = _levels(monitor.drift_findings(_rows(5, 1), _rows(5, 2)))
    assert lv["확인 필요 비율"] == "부족"
    hb = [{"kind": "approved"}] * 28 + [{"kind": "rejected"}] * 2
    hr = [{"kind": "approved"}] * 15 + [{"kind": "rejected"}] * 15 + [{"kind": "edited"}] * 3
    lv = _levels(monitor.drift_findings([], [], hb, hr))
    assert lv["반려율"] == "경보" and lv["승인 전 수정률"] == "주의"       # 수정 10%p 차이, 표본이 작아 유의하지 않음


def test_stats_helpers():
    assert round(monitor.chi2_sf(3.841, 1), 3) == 0.05 and round(monitor.chi2_sf(11.07, 5), 3) == 0.05
    assert monitor.psi({"a": 50, "b": 50}, {"a": 50, "b": 50}) == 0
    assert monitor.chi2_table({"x": 10}, {"x": 5}) == (0.0, 0, 1.0)


def test_windows_start_after_promotion_and_do_not_overlap():
    rows = [{"at": f"2026-10-21T10:{i:02d}:00", "i": i} for i in range(50)]
    base, recent = monitor.windows(rows, 10, start="2026-10-21T10:05:00")
    assert [r["i"] for r in base] == list(range(5, 15)) and [r["i"] for r in recent] == list(range(40, 50))
    base, recent = monitor.windows(rows[:12], 10)
    assert len(base) == 10 and len(recent) == 2


def test_report_end_to_end(tmp_path):
    ms, rec, reg = MetricsStore(tmp_path / "m.db"), MeetingRecords(tmp_path / "l.db"), PR.Registry(tmp_path / "r.json")
    e = reg.register(_comp(0.5))
    reg.promote(e["id"])
    for r in _rows(40, 1):
        ms.record({**r, "at": "2099-01-01T00:00:00", "version": e["id"], "source": "chat"})
    rep = monitor.report(ms, rec, reg)
    assert rep["champion"]["id"] == e["id"] and rep["window"]["base"]["count"] == 30
    assert rep["window"]["recent"]["count"] == 10 and rep["versions"][0]["requests"] == 40


# ---------------------------------------------------------------- 정책 개정
def test_policy_revision_flags_decisions_approved_under_old_rules(tmp_path):
    store = LedgerStore(tmp_path / "l.db")
    store.load_seed()
    rec = MeetingRecords(tmp_path / "l.db")
    out = monitor.policy_revision_review(store, rec, "v2", "v3")
    ids = {d["decision_id"] for d in out}
    assert "CSB-0004" in ids                                       # 실시간 전송 조항(ARC-02) 신설 전 승인
    assert all(d["label"] == "재검토 필요" and d["effective_from"] < "2026-07-01" for d in out)
    assert not any(d["decision_id"].startswith("OPS-") for d in out)     # 새 정책 시행 뒤 승인은 대상 아님
    assert monitor.policy_revision_review(store, rec, "v3", "v3") == []
