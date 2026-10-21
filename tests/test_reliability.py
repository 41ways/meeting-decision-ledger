"""신뢰도 데이터셋·채점 규칙 고정 — LLM 없이 데이터셋 검사, 원장 번호 시뮬레이션, 질문 채점, 신뢰구간을 확인한다."""
# pylint: disable=missing-function-docstring,import-error,wrong-import-position  # eval/reliability를 경로에 넣어 읽는다
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval" / "reliability"))

import dataset as D  # noqa: E402
import run as R  # noqa: E402


def test_dataset_scale_and_consistency():
    err, _, st = D.check(D.load_gold())
    assert err == []
    for s in ("dev", "test"):
        assert {k: st["stats"][s][k] for k in D.TARGET} == D.TARGET


def test_ledger_ids_are_deterministic():
    g = D.load_gold()
    assert D.id_map(g) == D.id_map(g)
    led = D.Ledger()
    assert led.next_id("EXP") == "EXP-0008"        # 시드 EXP-0001~0007 다음


def test_answer_rules():
    q = {"expected_state": "확정", "expected_ids": ["OPS-0018"], "expected_keywords": ["60초"],
         "forbidden_keywords": ["30초"]}
    assert R.answer_ok(q, {"final": "[확정] 지금 기준은 OPS-0018입니다.", "label": "확정"}, {})
    assert not R.answer_ok(q, {"final": "[확정] 30초에서 OPS-0018로 바뀌었습니다.", "label": "확정"}, {})
    assert not R.answer_ok(q, {"final": "[미결] OPS-0018 논의 중", "label": "미결"}, {})
    nf = {"expected_state": "찾지 못함", "expected_ids": []}
    assert R.answer_ok(nf, {"final": "[찾지 못함] 관련 결정이나 논의를 찾지 못했습니다.", "label": "찾지 못함"}, {})
    assert not R.answer_ok(nf, {"final": "[확정] OPS-0007입니다.", "label": "확정"}, {})
    day = {"expected_state": "확정", "expected_ids": [], "expected_keywords": ["12월 7일"]}
    assert R.answer_ok(day, {"final": "[확정] 시범 운영은 2026-12-07입니다.", "label": "확정"}, {})


def test_intervals():
    lo, hi = R.wilson(8, 10)
    assert 0.49 < lo < 0.5 and 0.94 < hi < 0.95
    b = R.boot_rate([(3, 5), (4, 4), (1, 6), (5, 5)])
    assert b["k"] == 13 and b["n"] == 20 and b["lo"] <= b["pct"] <= b["hi"]
    assert R.rate(0, 0)["pct"] is None
