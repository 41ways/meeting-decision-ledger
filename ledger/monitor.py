"""드리프트 감지 — 기준선 창과 최근 창을 비교해 경보를 낸다 (docs/pipeline_ops.md §3).

- 입력 드리프트: 검색 1위 점수 분포(KS 검정 + PSI), 새 주제 비율, 검색 하한 미달 비율, 낯선 낱말 비율
- 동작 드리프트: 라벨 분포(카이제곱·PSI), 확인 필요 비율, 파싱 실패율, 재작성 비율, 심판 저점 비율,
                전체 읽기 대비책 비율, 오류율, 모드별 응답 시간 중앙값 비율
- 사람 드리프트: 반려율·수정률 (사람이 고친 만큼 파이프라인이 틀렸다 — 가장 강한 신호)

기준선 창 = champion이 승격된 직후 N건, 최근 창 = 지금 돌고 있는 버전의 마지막 N건(기준선과 겹치지 않게).
비율·분포는 차이가 효과 크기 이상이면서 p<0.01이면 '경보', p<0.05면 '주의' (지표가 많아 문턱을 나눴다).
정책 개정 감지는 지난번 본 정책 버전과 지금 버전이 다르면 옛 조항을 근거로 승인된 결정을 '재검토 필요'로 낸다.
"""
from __future__ import annotations

import json
import math
import re
from statistics import mean, median

from .metrics import LABELS, human_rates, human_signals

# 판정 기준 — 바꾸면 docs/pipeline_ops.md §3 표도 함께 고친다
RULES = {
    "window": 30,            # 창 크기 (요청 수)
    "human_window": 20,      # 사람 신호 창 크기 (승인·반려·수정·닫기 건수)
    "min_n": 10,             # 창마다 최소 표본 (부족하면 판정하지 않음)
    "min_human": 5,          # 사람 신호 최소 결정 수 (승인+반려)
    # 지표가 열네 개라 0.05 하나로는 안정된 창에서도 자주 울린다 — 경보는 0.01, 주의는 0.05
    "alpha_alert": 0.01, "alpha_warn": 0.05,
    "human_alpha_alert": 0.05, "human_alpha_warn": 0.20,   # 사람 신호는 드물지만 가장 강한 신호라 문턱을 낮춘다
    "psi_warn": 0.10, "psi_alert": 0.25,       # PSI 관례값: 0.1 미만 안정, 0.25 이상 큰 이동
    "effect": 0.10,          # 비율 차이 효과 크기 (10%p)
    "effect_parse": 0.02,    # 파싱 실패는 호출당 비율이라 작게
    "effect_error": 0.05,
    "oov_warn": 0.10, "oov_alert": 0.20,       # 낯선 낱말 비율 평균 차이
    "time_warn": 1.15, "time_alert": 1.30,     # 응답 시간 중앙값 비율
}
SCORE_EDGES = (0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 1.01)   # 임계값(0.50·0.55·0.58) 주변을 촘촘히
LEVELS = ("정상", "주의", "경보", "부족")


# ---------------------------------------------------------------- 통계
def _gamma_q(a: float, x: float) -> float:
    """
    정규화 상부 불완전 감마 Q(a, x) — 카이제곱 p값용 (scipy 없이)
    Args:
        a: 모수 (자유도/2)
        x: 값 (통계량/2)
    Returns:
        Q(a, x)
    """
    if x <= 0:
        return 1.0
    lg = math.lgamma(a)
    if x < a + 1:                         # 급수 전개가 빨리 수렴하는 구간
        term = total = 1.0 / a
        n = a
        for _ in range(500):
            n += 1
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-12:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - lg))
    # 연분수 전개 (Lentz)
    b, c, d = x + 1 - a, 1e300, 1 / (x + 1 - a)
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - lg) * h)


def chi2_sf(stat: float, df: int) -> float:
    """
    카이제곱 분포의 위꼬리 확률 (p값)
    Args:
        stat: 카이제곱 통계량
        df:   자유도
    Returns:
        p값
    Examples:
        round(chi2_sf(3.841, 1), 3) -> 0.05
    """
    return _gamma_q(df / 2, stat / 2) if df > 0 else 1.0


def chi2_table(base: dict[str, int], recent: dict[str, int]) -> tuple[float, int, float]:
    """
    두 창의 범주 분포가 같은지 카이제곱 동질성 검정 (2×k 표)
    Args:
        base:   기준선 범주별 건수
        recent: 최근 범주별 건수
    Returns:
        (통계량, 자유도, p값). 범주가 하나뿐이거나 비어 있으면 (0, 0, 1)
    """
    cats = [k for k in set(base) | set(recent) if base.get(k, 0) + recent.get(k, 0) > 0]
    n1, n2 = sum(base.get(k, 0) for k in cats), sum(recent.get(k, 0) for k in cats)
    if len(cats) < 2 or not n1 or not n2:
        return 0.0, 0, 1.0
    stat = 0.0
    for k in cats:
        col = base.get(k, 0) + recent.get(k, 0)
        for obs, n in ((base.get(k, 0), n1), (recent.get(k, 0), n2)):
            exp = col * n / (n1 + n2)
            stat += (obs - exp) ** 2 / exp
    df = len(cats) - 1
    return round(stat, 3), df, chi2_sf(stat, df)


def two_prop(a: int, n1: int, b: int, n2: int) -> tuple[float, float, float]:
    """
    두 비율 비교 (2×2 카이제곱 = 두 비율 z 검정의 제곱)
    Args:
        a:  기준선 해당 건수
        n1: 기준선 전체
        b:  최근 해당 건수
        n2: 최근 전체
    Returns:
        (기준선 비율, 최근 비율, p값)
    """
    p1, p2 = a / n1, b / n2
    pool = (a + b) / (n1 + n2)
    if pool in (0, 1):
        return p1, p2, 1.0
    z2 = (p1 - p2) ** 2 / (pool * (1 - pool) * (1 / n1 + 1 / n2))
    return p1, p2, chi2_sf(z2, 1)


def ks_test(a: list[float], b: list[float]) -> tuple[float, float]:
    """
    두 표본 콜모고로프-스미르노프 검정 — 점수 분포가 같은지 (점근 p값)
    Args:
        a: 기준선 점수
        b: 최근 점수
    Returns:
        (D 통계량, p값)
    """
    xs, ys = sorted(a), sorted(b)
    i = j = 0
    d = 0.0
    while i < len(xs) and j < len(ys):
        v = min(xs[i], ys[j])
        while i < len(xs) and xs[i] == v:
            i += 1
        while j < len(ys) and ys[j] == v:
            j += 1
        d = max(d, abs(i / len(xs) - j / len(ys)))
    ne = len(xs) * len(ys) / (len(xs) + len(ys))
    lam = (math.sqrt(ne) + 0.12 + 0.11 / math.sqrt(ne)) * d
    p = 2 * sum((-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam) for k in range(1, 101))
    return round(d, 4), min(max(p, 0.0), 1.0)


def psi(base: dict[str, float], recent: dict[str, float]) -> float:
    """
    PSI(모집단 안정 지수) — 분포가 얼마나 옮겨 갔나. Σ (r−b)·ln(r/b)
    Args:
        base:   기준선 범주(구간)별 건수
        recent: 최근 범주(구간)별 건수
    Returns:
        PSI (0이면 같음). 빈 구간은 0.5건으로 보정한다
    """
    cats = set(base) | set(recent)
    n1, n2 = sum(base.values()) or 1, sum(recent.values()) or 1
    total = 0.0
    for k in cats:
        b = max(base.get(k, 0), 0.5) / n1
        r = max(recent.get(k, 0), 0.5) / n2
        total += (r - b) * math.log(r / b)
    return round(total, 4)


def bin_counts(values: list[float], edges: tuple[float, ...] = SCORE_EDGES) -> dict[str, int]:
    """
    점수를 구간별 건수로
    Args:
        values: 점수 목록
        edges:  구간 경계 (왼쪽 포함)
    Returns:
        '하한~상한' → 건수
    """
    out = {f"{lo:.2f}~{hi:.2f}": 0 for lo, hi in zip(edges, edges[1:])}
    for v in values:
        for lo, hi in zip(edges, edges[1:]):
            if lo <= v < hi:
                out[f"{lo:.2f}~{hi:.2f}"] += 1
                break
    return out


# ---------------------------------------------------------------- 판정
def _finding(group: str, metric: str, base, recent, test: str, level: str, rule: str) -> dict:
    """판정 한 줄 (화면·보고서 공통 형식)"""
    return {"group": group, "metric": metric, "base": base, "recent": recent, "test": test, "level": level,
            "rule": rule}


def _prop_finding(group: str, metric: str, a: int, n1: int, b: int, n2: int, effect: float,
                  min_n: int, alpha: tuple[float, float]) -> dict:
    """
    비율 지표 판정
    Args:
        group, metric: 묶음·지표 이름
        a, n1:  기준선 해당·전체
        b, n2:  최근 해당·전체
        effect: 효과 크기 (비율 차이)
        min_n:  창마다 최소 표본
        alpha:  (경보 유의수준, 주의 유의수준)
    Returns:
        판정 dict
    """
    rule = f"차이 ≥ {effect:.0%}p 이고 p < {alpha[0]} 경보, p < {alpha[1]} 주의"
    if n1 < min_n or n2 < min_n:
        return _finding(group, metric, f"{a}/{n1}", f"{b}/{n2}", "표본 부족", "부족", rule)
    p1, p2, p = two_prop(a, n1, b, n2)
    diff = p2 - p1
    level = "정상" if abs(diff) < effect or p >= alpha[1] else ("경보" if p < alpha[0] else "주의")
    return _finding(group, metric, round(p1, 3), round(p2, 3), f"차이 {diff:+.1%}p, p={p:.3f}", level, rule)


def _sum(rows: list[dict], key: str) -> int:
    """행들의 정수 칸 합 (None은 0)"""
    return sum(r.get(key) or 0 for r in rows)


def _label_counts(rows: list[dict]) -> dict[str, int]:
    """행들의 라벨 건수 합"""
    out: dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("labels") or {}).items():
            out[k] = out.get(k, 0) + v
    return out


def drift_findings(base: list[dict], recent: list[dict], base_human: list[dict] | None = None,
                   recent_human: list[dict] | None = None, rules: dict | None = None) -> list[dict]:
    """
    기준선 창과 최근 창을 비교해 지표별 판정을 낸다
    Args:
        base:         기준선 창 요청 지표 (MetricsStore.rows 형식)
        recent:       최근 창 요청 지표
        base_human:   기준선 사람 신호 (human_signals 형식)
        recent_human: 최근 사람 신호
        rules:        판정 기준 (기본 RULES)
    Returns:
        판정 목록 (group, metric, base, recent, test, level, rule)
    """
    R = {**RULES, **(rules or {})}  # pylint: disable=invalid-name
    mn, out = R["min_n"], []
    al, ah = (R["alpha_alert"], R["alpha_warn"]), (R["human_alpha_alert"], R["human_alpha_warn"])
    b_ok = [r for r in base if not r.get("error")]
    r_ok = [r for r in recent if not r.get("error")]

    # ---- 입력
    s1 = [s for r in b_ok for s in r.get("top1") or []]
    s2 = [s for r in r_ok for s in r.get("top1") or []]
    # PSI는 작은 창에서 우연히도 0.1을 넘는다 — KS 검정이 유의할 때만 PSI 크기로 등급을 매긴다
    rule = f"PSI ≥ {R['psi_alert']}·KS p < {al[0]} 경보, PSI ≥ {R['psi_warn']}·KS p < {al[1]} 주의"
    if len(s1) < mn or len(s2) < mn:
        out.append(_finding("입력", "검색 1위 점수 분포", len(s1), len(s2), "표본 부족", "부족", rule))
    else:
        v = psi(bin_counts(s1), bin_counts(s2))
        d, p = ks_test(s1, s2)
        level = "경보" if v >= R["psi_alert"] and p < al[0] else \
            "주의" if v >= R["psi_warn"] and p < al[1] else "정상"
        out.append(_finding("입력", "검색 1위 점수 분포", f"중앙값 {median(s1):.3f}", f"중앙값 {median(s2):.3f}",
                            f"PSI {v:.3f}, KS D={d}, p={p:.3f}", level, rule))
    out.append(_prop_finding("입력", "새 주제(주제 없음) 비율", _sum(b_ok, "no_topic"), _sum(b_ok, "items"),
                             _sum(r_ok, "no_topic"), _sum(r_ok, "items"), R["effect"], mn, al))
    out.append(_prop_finding("입력", "검색 하한 미달 비율", _sum(b_ok, "below_floor"), _sum(b_ok, "items"),
                             _sum(r_ok, "below_floor"), _sum(r_ok, "items"), R["effect"], mn, al))
    o1 = [r["oov_ratio"] for r in b_ok if r.get("oov_ratio") is not None]
    o2 = [r["oov_ratio"] for r in r_ok if r.get("oov_ratio") is not None]
    rule = f"평균 차이 ≥ {R['oov_alert']:.0%}p 경보, ≥ {R['oov_warn']:.0%}p 주의"
    if len(o1) < mn or len(o2) < mn:
        out.append(_finding("입력", "용어집·별칭에 없는 낱말 비율", len(o1), len(o2), "표본 부족", "부족", rule))
    else:
        d = mean(o2) - mean(o1)
        out.append(_finding("입력", "용어집·별칭에 없는 낱말 비율", round(mean(o1), 3), round(mean(o2), 3),
                            f"차이 {d:+.1%}p", "경보" if abs(d) >= R["oov_alert"] else
                            "주의" if abs(d) >= R["oov_warn"] else "정상", rule))

    # ---- 동작
    l1, l2 = _label_counts(b_ok), _label_counts(r_ok)
    rule = f"PSI ≥ {R['psi_warn']} 이고 χ² p < {al[0]} 경보, p < {al[1]} 주의"
    if sum(l1.values()) < mn or sum(l2.values()) < mn:
        out.append(_finding("동작", "라벨 분포", l1, l2, "표본 부족", "부족", rule))
    else:
        stat, df, p = chi2_table(l1, l2)
        v = psi({k: l1.get(k, 0) for k in LABELS}, {k: l2.get(k, 0) for k in LABELS})
        level = "정상" if v < R["psi_warn"] or p >= al[1] else "경보" if p < al[0] else "주의"
        out.append(_finding("동작", "라벨 분포", l1, l2, f"χ²={stat}(df {df}), p={p:.3f}, PSI {v:.3f}", level, rule))
    out.append(_prop_finding("동작", "확인 필요 비율", l1.get("확인 필요", 0), sum(l1.values()),
                             l2.get("확인 필요", 0), sum(l2.values()), R["effect"], mn, al))
    out.append(_prop_finding("동작", "JSON 파싱 실패율(호출당)", _sum(b_ok, "parse_errors"), _sum(b_ok, "llm_calls"),
                             _sum(r_ok, "parse_errors"), _sum(r_ok, "llm_calls"), R["effect_parse"], mn, al))
    q1 = [r for r in b_ok if r.get("mode") == "question"]
    q2 = [r for r in r_ok if r.get("mode") == "question"]
    out.append(_prop_finding("동작", "재작성 발생 비율(질의)", sum(1 for r in q1 if r.get("rewrites")), len(q1),
                             sum(1 for r in q2 if r.get("rewrites")), len(q2), R["effect"], mn, al))
    j1 = [r["judge_min"] for r in b_ok if r.get("judge_min") is not None]
    j2 = [r["judge_min"] for r in r_ok if r.get("judge_min") is not None]
    out.append(_prop_finding("동작", "심판 저점(3점 이하) 비율", sum(1 for x in j1 if x <= 3), len(j1),
                             sum(1 for x in j2 if x <= 3), len(j2), R["effect"], mn, al))
    out.append(_prop_finding("동작", "전체 읽기 대비책 사용 비율", sum(1 for r in b_ok if r.get("full_read")), len(b_ok),
                             sum(1 for r in r_ok if r.get("full_read")), len(r_ok), R["effect"], mn, al))
    out.append(_prop_finding("동작", "오류율", sum(1 for r in base if r.get("error")), len(base),
                             sum(1 for r in recent if r.get("error")), len(recent), R["effect_error"], mn, al))
    # 모드마다 처리량이 크게 달라(회의 60초, 질의 20초) 섞으면 모드 비율만 바뀌어도 시간이 바뀐 것처럼 보인다
    rule = f"중앙값 비율 ≥ {R['time_alert']} 경보, ≥ {R['time_warn']} 주의"
    for mode in sorted({r.get("mode") for r in b_ok} & {r.get("mode") for r in r_ok}):
        t1 = [r["seconds"] for r in b_ok if r.get("mode") == mode and r.get("seconds") is not None]
        t2 = [r["seconds"] for r in r_ok if r.get("mode") == mode and r.get("seconds") is not None]
        if len(t1) < mn or len(t2) < mn or not median(t1):
            continue
        ratio = median(t2) / median(t1)
        out.append(_finding("동작", f"응답 시간 중앙값({mode})", round(median(t1), 1), round(median(t2), 1),
                            f"비율 {ratio:.2f}", "경보" if ratio >= R["time_alert"] else
                            "주의" if ratio >= R["time_warn"] else "정상", rule))

    # ---- 사람
    h1, h2 = human_rates(base_human or []), human_rates(recent_human or [])
    mh = R["min_human"]
    out.append(_prop_finding("사람", "반려율", h1["rejected"], h1["decided"], h2["rejected"], h2["decided"],
                             R["effect"], mh, ah))
    out.append(_prop_finding("사람", "승인 전 수정률", h1["edited"], h1["decided"], h2["edited"], h2["decided"],
                             R["effect"], mh, ah))
    return out


def windows(rows: list[dict], n: int, start: str | None = None,
            recent_rows: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """
    기준선 창과 최근 창을 자른다 (겹치지 않게)
    Args:
        rows:        기준선 버전의 행 (오래된 순)
        n:           창 크기
        start:       기준선 시작 시각 (승격 시각). 이전 행은 버린다
        recent_rows: 최근 창을 다른 버전에서 뽑을 때 그 버전의 행. None이면 rows에서 뽑는다
    Returns:
        (기준선, 최근)
    """
    base_pool = [r for r in rows if not start or r["at"] >= start]
    base = base_pool[:n]
    if recent_rows is None:
        recent = base_pool[len(base):][-n:]
    else:
        ids = {id(r) for r in base}
        recent = [r for r in recent_rows if id(r) not in ids][-n:]
    return base, recent


# ---------------------------------------------------------------- 정책 개정
def policy_revision_review(store, records, old_version: str, new_version: str) -> list[dict]:
    """
    정책 개정으로 다시 봐야 할 승인 결정 — 옛 버전에서 바뀐 조항을 근거로 승인된 유효 결정

    '근거로 삼았다'는 기록으로 판단한다: 조항과 같은 주제, 결정 문장·사유·승인자·부가 정보에 조항 ID가 적힘,
    회의 기록의 판정(충돌·변경·예외·안내)에 조항 ID나 그 조항의 예외 ID가 남음.
    새 버전 시행일 전에 승인된 결정만 대상이다 (그 뒤 승인은 새 정책으로 판정받았다).
    Args:
        store:       LedgerStore (정책·결정·예외)
        records:     MeetingRecords (결정에 연결된 회의 기록 항목)
        old_version: 옛 정책 버전 (예: v2)
        new_version: 새 정책 버전 (예: v3)
    Returns:
        [{decision_id, project_id, topic, statement, effective_from, label: '재검토 필요',
          rules: [{rule_id, change(개정|신설|삭제), how}]}]
    """
    rules = store.rules(current_only=False)
    old = {r["rule_id"]: r for r in rules if r["policy_version"] == old_version}
    new = {r["rule_id"]: r for r in rules if r["policy_version"] == new_version}
    changed = {}
    for rid in set(old) | set(new):
        o, n = old.get(rid), new.get(rid)
        if o and not n:
            changed[rid] = "삭제"
        elif n and not o:
            changed[rid] = "신설"
        elif any(o.get(k) != n.get(k) for k in ("text", "approver_role", "threshold", "topic", "domain")):
            changed[rid] = "개정"
    if not changed:
        return []
    starts = [r.get("effective_from") or "" for r in new.values()]
    new_from = max(starts) if starts else ""
    waivers = {w["waiver_id"]: w["rule_id"] for w in store.all_waivers()}
    try:
        # 결정 주제 → 권한표 주제 (그래프와 같은 표). 그래프를 못 읽어도 나머지 근거로 판단한다
        from agent.ledger_graph import AUTH_TOPIC  # pylint: disable=import-outside-toplevel
    except ImportError:
        AUTH_TOPIC = {}  # pylint: disable=invalid-name
    out = []
    for d in store.decisions():
        if d["status"] != "approved" or not d.get("is_active") or (d.get("effective_from") or "") >= new_from:
            continue
        text = " ".join(str(d.get(k) or "") for k in ("statement", "rationale", "approved_by")) \
            + json.dumps(d.get("extra") or {}, ensure_ascii=False)
        review = " ".join(json.dumps(i.get("review") or {}, ensure_ascii=False)
                          for i in records.items_for_decision(d["decision_id"]))
        hits = []
        for rid, change in sorted(changed.items()):
            r = new.get(rid) or old.get(rid)
            how = []
            if r.get("topic") in (d["topic"], AUTH_TOPIC.get(d["topic"])):
                how.append("주제 일치")
            if re.search(rf"(?<![A-Z]){re.escape(rid)}(?!\d)", text):
                how.append("결정 기록에 조항 언급")
            if re.search(rf"(?<![A-Z]){re.escape(rid)}(?!\d)", review) or \
                    any(wid in review or wid in text for wid, wr in waivers.items() if wr == rid):
                how.append("회의 판정 근거")
            if how:
                hits.append({"rule_id": rid, "change": change, "how": " · ".join(how)})
        if hits:
            out.append({"decision_id": d["decision_id"], "project_id": d["project_id"], "topic": d["topic"],
                        "statement": d["statement"], "effective_from": d.get("effective_from"),
                        "label": "재검토 필요", "rules": hits})
    return out


# ---------------------------------------------------------------- 보고서
def report(metrics, records, registry, store=None, running: str | None = None, rules: dict | None = None) -> dict:
    """
    감시 보고서 — 관리자 콘솔 '품질 감시' 탭과 scripts/drift_report.py가 같은 것을 쓴다
    Args:
        metrics:  MetricsStore
        records:  MeetingRecords
        registry: pipeline_registry.Registry
        store:    LedgerStore (정책 개정 감지용, None이면 건너뜀)
        running:  지금 돌고 있는 버전 ID (None이면 champion)
        rules:    판정 기준 덮어쓰기
    Returns:
        {running, champion, window, findings, alerts, warnings, human, versions, policy}
    """
    R = {**RULES, **(rules or {})}  # pylint: disable=invalid-name
    champ = registry.champion()
    run_id = running or (champ or {}).get("id")
    base_id = (champ or {}).get("id") or run_id
    start = registry.promoted_at(base_id) if champ else None
    base_rows = metrics.rows(version=base_id) if base_id else []
    recent_rows = metrics.rows(version=run_id) if run_id and run_id != base_id else None
    base, recent = windows(base_rows, R["window"], start, recent_rows)

    signals = human_signals(records.events(), records.meetings(), records.items_for_decision,
                            metrics.decision_versions())
    hb_pool = [s for s in signals if s["version"] == base_id and (not start or s["at"] >= start)]
    hb = hb_pool[:R["human_window"]]
    hr = ([s for s in signals if s["version"] == run_id] if run_id != base_id else hb_pool[len(hb):])
    hr = hr[-R["human_window"]:]
    findings = drift_findings(base, recent, hb, hr, R)

    by_ver: dict[str, list] = {}
    for s in signals:
        by_ver.setdefault(s["version"], []).append(s)
    counts = {x["version"]: x for x in metrics.summary()}
    versions = [{"id": v["id"], "status": v["status"], "registered_at": v["registered_at"],
                 "promoted_at": v.get("promoted_at"), "commit": (v.get("git") or {}).get("commit"),
                 "requests": (counts.get(v["id"]) or {}).get("n", 0),
                 "avg_sec": (counts.get(v["id"]) or {}).get("avg_sec"),
                 "eval": (v.get("evals") or [{}])[-1].get("verdict")} for v in registry.versions()]
    policy = None
    if store is not None:
        cur, seen = store.current_policy_version(), registry.data.get("policy_seen")
        policy = {"current": cur, "seen": seen, "changed": bool(seen and seen != cur),
                  "review": policy_revision_review(store, records, seen, cur) if seen and seen != cur else []}

    def span(rows):
        return {"count": len(rows), "from": rows[0]["at"] if rows else None, "to": rows[-1]["at"] if rows else None}
    return {"running": {"id": run_id, "status": (registry.get(run_id) or {}).get("status")},
            "champion": {"id": champ["id"], "promoted_at": champ.get("promoted_at")} if champ else None,
            "window": {"size": R["window"], "base": {"version": base_id, **span(base)},
                       "recent": {"version": run_id, **span(recent)}},
            "findings": findings,
            "alerts": sum(f["level"] == "경보" for f in findings) + bool(policy and policy["changed"]),
            "warnings": sum(f["level"] == "주의" for f in findings),
            "human": {v: human_rates(s) for v, s in by_ver.items()},
            "versions": versions, "policy": policy}
