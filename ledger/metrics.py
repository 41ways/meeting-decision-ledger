"""요청별 지표 기록 — 그래프가 끝난 상태에서 뽑아 store/metrics.db에 쌓는다 (docs/pipeline_ops.md §2).

파이프라인 동작은 바꾸지 않는다. 노드가 남긴 이벤트·usage·보고서를 읽기만 한다.
- requests:          요청 1건 = 1행 (모드, 라벨, 확인 필요 사유, 검색 점수, 파싱 실패, 재작성, 심판 점수, 시간, 호출·토큰, 버전)
- decision_versions: 원장에 올린 결정이 어느 버전에서 나왔는가 (사람 신호를 버전별로 묶을 때 쓴다)

사람 신호(승인·반려·수정·미결 닫기)는 회의 기록 이력(records.events)에 이미 있으므로 따로 저장하지 않고 읽을 때 묶는다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .store import ROOT, STORE_DIR

DEFAULT_DB = STORE_DIR / "metrics.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, version TEXT, source TEXT, mode TEXT, user TEXT, model TEXT,
    cached INTEGER NOT NULL DEFAULT 0, error TEXT,
    seconds REAL, llm_calls INTEGER, tokens INTEGER, parse_errors INTEGER,
    rewrites INTEGER, judge_min INTEGER,
    items INTEGER, no_topic INTEGER, below_floor INTEGER,
    full_read INTEGER, oov_ratio REAL,
    labels TEXT,                     -- JSON {라벨: 건수}
    check_kinds TEXT,                -- JSON {확인 필요 사유 종류: 건수}
    top1 TEXT,                       -- JSON [항목별 검색 1위 점수]
    proposed TEXT,                   -- JSON [원장에 올린 결정 ID]
    node_ms TEXT                     -- JSON {노드: 밀리초} (스트리밍 요청만, 다시 돈 노드는 합)
);
CREATE INDEX IF NOT EXISTS requests_version ON requests(version, id);
CREATE TABLE IF NOT EXISTS decision_versions (
    decision_id TEXT PRIMARY KEY, version TEXT, at TEXT
);
"""
JSON_COLS = ("labels", "check_kinds", "top1", "proposed", "node_ms")
LABELS = ("확정", "승인 대기", "미결", "확인 필요", "찾지 못함")

# 확인 필요 사유를 몇 가지 종류로 묶는다 — 문구가 조금 바뀌어도 종류 비율은 비교할 수 있게. 앞 규칙이 우선
CHECK_KINDS = [("판정 실패", r"판정 실패|해석 불가"), ("조회 실패", r"조회 실패"),
               ("규정 충돌", r"규정 충돌"), ("권한 초과", r"권한 초과|권한"),
               ("기존 결정과 다름", r"기존 결정과 다름"), ("기준 문서와 다름", r"기준 문서와 다름"),
               ("주제 불확실", r"주제 불확실|주제를 잘못|멀다"), ("비슷한 다른 결정", r"비슷한 결정|같은 사안"),
               ("고른 결정 불일치", r"고른 결정|고르지 않음")]
WORD = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9.]+")
# 어느 문장에나 나오는 말 — 낯선 낱말 비율에서 뺀다 (분모·분자 모두)
COMMON = {"회의", "정리", "정리해줘", "요약", "요약해줘", "결정", "어떻게", "하기로", "했어", "했어요", "했나요", "왜", "누가",
          "언제", "이유", "바꾸면", "어때", "그럼", "그래서", "우리", "이거", "그거", "지금", "다시", "해주세요", "해줘",
          "있어", "없어", "하자", "쓰면", "어떨까", "정해졌어", "골랐어", "말고", "그냥", "그리고", "하는", "있는", "이번"}


def _now() -> str:
    """현재 시각 (초 단위 ISO 문자열)"""
    return datetime.now().isoformat(timespec="seconds")


def check_kind(reason: str) -> str:
    """
    확인 필요 사유 문구를 종류로 묶는다
    Args:
        reason: 라벨 이유·확인 필요 사유 문구
    Returns:
        종류 이름 (맞는 규칙이 없으면 '기타')
    Examples:
        check_kind('규정 충돌 · 권한 초과') -> '규정 충돌'
    """
    return next((k for k, pat in CHECK_KINDS if re.search(pat, reason or "")), "기타")


def top1_score(research: dict) -> float | None:
    """
    항목 하나의 검색 1위 점수 — 주제 자료·정책·기준 문서·원문·다른 주제 결정 중 가장 가까운 것
    Args:
        research: resolve_current 결과 (topic_support, rules, baseline, discussion, similar_other)
    Returns:
        0~1 점수. 잰 값이 하나도 없으면 None
    """
    vals = [research.get("topic_support")]
    for k in ("rules", "baseline", "discussion"):
        vals += [x.get("_score") for x in research.get(k) or []]
    vals += [x.get("score") for x in research.get("similar_other") or []]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(max(vals), 4) if vals else None


def vocab_words(root: Path = ROOT) -> set[str]:
    """
    아는 낱말 — 주제 목록(키·설명), 용어집 전체 칸, 별칭 파일
    Args:
        root: 저장소 루트
    Returns:
        2자 이상 낱말 집합 (영문은 소문자)
    """
    texts = []
    topics = root / "data/project/topics.json"
    if topics.exists():
        t = json.loads(topics.read_text(encoding="utf-8")).get("topics", {})
        texts += [f"{k} {v}" for k, v in t.items()]
    gl = root / "data/project/r2_docs/용어집.md"
    if gl.exists():
        texts.append(gl.read_text(encoding="utf-8"))
    for p in list(root.glob("data/project/**/*alias*.json")) + list(root.glob("data/project/**/*alias*.md")):
        texts.append(p.read_text(encoding="utf-8"))
    return {w.lower() for t in texts for w in WORD.findall(t)}


_VOCAB: set[str] | None = None


def oov_ratio(text: str, vocab: set[str] | None = None) -> float | None:
    """
    용어집·주제·별칭에 없는 낱말 비율 (입력 드리프트 지표 — 절대값보다 변화를 본다)

    조사가 붙은 낱말('캐시를')도 아는 낱말로 시작하면 아는 것으로 본다.
    Args:
        text:  사용자 문장 또는 회의 발언 묶음
        vocab: 아는 낱말 집합. None이면 저장소 파일에서 한 번 읽어 둔다
    Returns:
        0~1 비율. 셀 낱말이 없으면 None
    """
    global _VOCAB  # pylint: disable=global-statement  # 파일을 요청마다 다시 읽지 않으려고 모듈에 둔다
    if vocab is None:
        if _VOCAB is None:
            _VOCAB = vocab_words()
        vocab = _VOCAB
    words = [w.lower() for w in WORD.findall(text or "") if w not in COMMON]
    if not words:
        return None
    known = sum(any(w.startswith(v) for v in vocab if len(v) >= 2 and v[0] == w[0]) for w in words)
    return round(1 - known / len(words), 4)


def extract(state: dict, seconds: float, version: str, source: str = "chat", user: str = "",
            model: str = "", cached: bool = False, error: str | None = None) -> dict:
    """
    그래프 최종 상태에서 요청 1건의 지표를 뽑는다 (상태를 바꾸지 않음)

    라벨: 질의는 답 라벨(확정·승인 대기·미결·확인 필요·찾지 못함), 회의·의견은 결정 후보 라벨(확인 필요·승인 대기)과 미결.
    Args:
        state:   그래프 최종 상태 (mode, events, usage, report, items, validation_attempts, segments, message)
        seconds: 응답 시간(초)
        version: 파이프라인 버전 ID
        source:  chat | process | eval
        user:    사용자 ID
        model:   모델 조합 키 (PRESETS)
        cached:  저장된 답을 돌려준 요청
        error:   실행 중 오류 문구
    Returns:
        requests 표 한 행 dict
    """
    # 지연 import — agent 패키지가 ledger를 import하므로 모듈 맨 위에서 부르면 순환한다
    from agent.retrieve import answer_label  # pylint: disable=import-outside-toplevel
    mode = state.get("mode") or ""
    report = state.get("report") or {}
    items = report.get("items") or state.get("items") or []
    events = state.get("events") or []
    usage = state.get("usage") or {}
    labels: dict[str, int] = {}
    kinds: dict[str, int] = {}

    def add(d: dict, k: str) -> None:
        d[k] = d.get(k, 0) + 1

    for it in items:
        res = it.get("research") or {}
        if mode == "question" and res:
            lab = answer_label(it)
            add(labels, lab)
            if lab == "확인 필요":
                for why in (it.get("check_reasons") or []) + (["주제 불확실"] if res.get("topic_doubt") else []) \
                        + (["비슷한 결정"] if res.get("similar_other") else []):
                    add(kinds, check_kind(why))
        elif it.get("label"):
            add(labels, it["label"])
            if it["label"] == "확인 필요":
                add(kinds, check_kind(it.get("label_reason") or ""))
    for _ in report.get("undecided") or state.get("undecided") or []:
        add(labels, "미결")
    scored = [it for it in items if it.get("research")]
    top1 = [s for s in (top1_score(it["research"]) for it in scored) if s is not None]
    below = sum(1 for it in scored if it["research"].get("authority") == "none" or it["research"].get("topic_doubt"))
    validations = [e for e in events if e.get("type") == "Validation"]
    judged = [min(e["judge"]["scores"].values()) for e in validations if (e.get("judge") or {}).get("scores")]
    text = state.get("message") or ""
    if mode in ("meeting", "interview") and state.get("segments"):
        text = " ".join(s.get("text", "") for s in state["segments"])
    return {"at": _now(), "version": version, "source": source, "mode": mode, "user": user, "model": model,
            "cached": int(bool(cached)), "error": error,
            "seconds": round(seconds, 2), "llm_calls": usage.get("llm_calls", 0), "tokens": usage.get("tokens", 0),
            "parse_errors": usage.get("parse_errors", 0),
            "rewrites": max(len(validations) - 1, 0), "judge_min": judged[-1] if judged else None,
            "items": len(items) if mode in ("meeting", "interview", "opinion", "question") else 0,
            "no_topic": sum(1 for it in items if mode in ("meeting", "opinion", "question") and not it.get("topic")),
            "below_floor": below,
            "full_read": sum(1 for e in events if e.get("type") == "FullReadFallback"),
            "oov_ratio": oov_ratio(text) if mode in ("meeting", "opinion", "question") else None,
            "labels": labels, "check_kinds": kinds, "top1": top1,
            "proposed": [x for x in report.get("proposed") or [] if x],
            "node_ms": {k: round(v) for k, v in (state.get("node_ms") or {}).items()}}


class MetricsStore:
    """
    요청 지표 저장소 (store/metrics.db)
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB):
        """
        DB를 열고 스키마 생성
        Args:
            db_path: SQLite 파일 경로
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # 칸을 나중에 더했다 — 먼저 만든 파일에도 붙인다
            if "node_ms" not in {r["name"] for r in c.execute("PRAGMA table_info(requests)")}:
                c.execute("ALTER TABLE requests ADD COLUMN node_ms TEXT")

    @contextmanager
    def _conn(self):
        """
        요청 단위 연결 — 정상 종료면 커밋
        Returns:
            sqlite3.Connection (row_factory=sqlite3.Row)
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, row: dict) -> int:
        """
        요청 1건 저장 (올린 결정은 버전 표에도 남긴다)
        Args:
            row: extract() 결과
        Returns:
            행 ID
        """
        cols = [k for k in row if k != "id"]
        vals = [json.dumps(row[k], ensure_ascii=False) if k in JSON_COLS else row[k] for k in cols]
        with self._conn() as c:
            cur = c.execute(f"INSERT INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            for did in row.get("proposed") or []:
                c.execute("INSERT OR IGNORE INTO decision_versions VALUES (?,?,?)",
                          (did, row.get("version"), row["at"]))
            return cur.lastrowid

    def link_decision(self, decision_id: str, version: str) -> None:
        """
        그래프 밖에서 원장에 올린 결정(의견 검토 → '결정으로 올리기')의 버전 기록
        Args:
            decision_id: 결정 ID
            version:     올릴 때 돌던 버전
        """
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO decision_versions VALUES (?,?,?)", (decision_id, version, _now()))

    def decision_versions(self) -> dict[str, str]:
        """
        결정 ID → 버전
        Returns:
            사전
        """
        with self._conn() as c:
            return {r["decision_id"]: r["version"] for r in c.execute("SELECT * FROM decision_versions")}

    def rows(self, version: str | None = None, since: str | None = None, include_cached: bool = False) -> list[dict]:
        """
        요청 지표 조회 (오래된 순)
        Args:
            version:        버전으로 한정
            since:          이 시각 이후만 (ISO)
            include_cached: 저장된 답을 돌려준 요청도 포함
        Returns:
            행 목록 (JSON 칸은 풀어서)
        """
        q, args = "SELECT * FROM requests WHERE 1=1", []
        if version:
            q, args = q + " AND version=?", [*args, version]
        if since:
            q, args = q + " AND at>=?", [*args, since]
        if not include_cached:
            q += " AND cached=0"
        with self._conn() as c:
            out = []
            for r in c.execute(q + " ORDER BY id", args):
                d = dict(r)
                for k in JSON_COLS:
                    empty = {} if k in ("labels", "check_kinds", "node_ms") else []
                    d[k] = json.loads(d[k]) if d.get(k) else empty
                out.append(d)
            return out

    def summary(self) -> list[dict]:
        """
        버전별 요청 수·평균 시간 (관리자 화면 요약 줄)
        Returns:
            [{version, n, first, last, avg_sec}]
        """
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT version, COUNT(*) n, MIN(at) first, MAX(at) last, ROUND(AVG(seconds), 1) avg_sec "
                "FROM requests WHERE cached=0 GROUP BY version ORDER BY first")]


HUMAN_KINDS = {"decision_approved": "approved", "decision_rejected": "rejected",
               "decision_edited": "edited", "item_closed": "closed"}


def human_signals(events: list[dict], meetings: list[dict], items_of, decision_versions: dict[str, str]) -> list[dict]:
    """
    사람 신호(승인·반려·승인 전 수정·미결 닫기)를 버전에 붙여 시간순으로

    결정 → 버전은 원장에 올린 요청의 버전(decision_versions)을 먼저 보고,
    없으면 그 결정이 나온 회의를 정리한 버전(meetings.pipeline_version)을 쓴다. 둘 다 없으면 '미상'.
    Args:
        events:            records.events() 결과
        meetings:          records.meetings() 결과
        items_of:          결정 ID → 연결된 항목 목록을 돌려주는 함수 (records.items_for_decision)
        decision_versions: MetricsStore.decision_versions() 결과
    Returns:
        [{at, kind(approved|rejected|edited|closed), target, version}]
    """
    mver = {m["meeting_id"]: m.get("pipeline_version") for m in meetings}
    out = []
    for e in events:
        kind = HUMAN_KINDS.get(e["kind"])
        if not kind:
            continue
        tgt = e.get("target") or ""
        ver = decision_versions.get(tgt)
        if not ver:
            # 미결 항목 ID는 '회의 ID:open:번호', 결정은 연결된 회의 기록 항목에서 회의를 찾는다
            mid = tgt.split(":")[0] if ":" in tgt else next((i["meeting_id"] for i in items_of(tgt)), None)
            ver = mver.get(mid) if mid else None
        out.append({"at": e["at"], "kind": kind, "target": tgt, "version": ver or "미상"})
    return out


def human_rates(signals: list[dict]) -> dict:
    """
    사람 신호 비율 — 반려율 = 반려/(승인+반려), 수정률 = 수정/(승인+반려)
    Args:
        signals: human_signals 결과 일부
    Returns:
        {approved, rejected, edited, closed, decided, reject_rate, edit_rate}
    """
    n = {k: sum(1 for s in signals if s["kind"] == k) for k in ("approved", "rejected", "edited", "closed")}
    decided = n["approved"] + n["rejected"]
    return {**n, "decided": decided,
            "reject_rate": round(n["rejected"] / decided, 3) if decided else None,
            "edit_rate": round(n["edited"] / decided, 3) if decided else None}
