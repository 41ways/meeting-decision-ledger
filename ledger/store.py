"""SQLite 저장소 — 결정 원장(R3), 정책(R4)·예외, 기준 문서 목록(R2)의 '진실의 원천'.

벡터 DB는 검색만 하고, "지금 유효한가"는 항상 여기서 판단한다.
- 원장은 추가만 한다(append-only). 뒤집힌 결정은 지우지 않고 superseded_by로 표시한다.
- 정책은 버전별로 보관하고 is_current=1인 버전만 판단에 쓴다.
- 예외(waiver)는 정책을 고치지 않는 별도 기록이며, 만료일이 지나면 효력이 없다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = Path(os.environ.get("LEDGER_STORE", ROOT / "store"))   # 평가는 별도 사본(LEDGER_STORE)으로 돈다
DEFAULT_DB = STORE_DIR / "ledger.db"
DATA = ROOT / "data" / "project"
PROJECTS = json.loads((DATA / "projects.json").read_text())["projects"] if (DATA / "projects.json").exists() else []


def project_key(project_id: str) -> str:
    """
    프로젝트 ID를 결정 ID 접두 약자로 변환
    Args:
        project_id: 프로젝트 ID (예: PRJ-OPSAI)
    Returns:
        projects.json의 key. 등록되지 않은 프로젝트는 기본값 "OPS"
    """
    return next((p["key"] for p in PROJECTS if p["project_id"] == project_id), "OPS")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    statement TEXT NOT NULL,
    rationale TEXT,
    alternatives_rejected TEXT,      -- JSON list
    decided_by TEXT,
    approved_by TEXT,
    status TEXT NOT NULL,            -- proposed | approved | rejected
    effective_from TEXT,
    superseded_by TEXT,
    evidence TEXT NOT NULL,          -- JSON list [{meeting_id, seq, quote}]
    extra TEXT                       -- JSON (rejection_reason, r2_updates 등)
);
CREATE TABLE IF NOT EXISTS policy_rules (
    policy_version TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    is_current INTEGER NOT NULL,
    effective_from TEXT,
    domain TEXT, topic TEXT, text TEXT, approver_role TEXT, threshold TEXT,
    PRIMARY KEY (policy_version, rule_id)
);
CREATE TABLE IF NOT EXISTS waivers (
    waiver_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    project_id TEXT,
    scope TEXT, approved_by TEXT, approved_at TEXT, expires_at TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT NOT NULL,
    doc_version TEXT NOT NULL,
    title TEXT, file TEXT, is_current INTEGER NOT NULL, updated_at TEXT, change_note TEXT,
    PRIMARY KEY (doc_id, doc_version)
);
"""

# decisions 테이블 컬럼 순서 — 위치 기반 INSERT의 placeholder 개수에 사용
DECISION_COLS = ["decision_id", "project_id", "topic", "statement", "rationale",
                 "alternatives_rejected", "decided_by", "approved_by", "status",
                 "effective_from", "superseded_by", "evidence", "extra"]


class LedgerStore:
    """
    결정 원장·정책·예외·기준 문서 목록을 담는 SQLite 저장소

    "지금 유효한가" 판단의 원천. 벡터 DB(LedgerRAG)는 검색 후보만 제공.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB):
        """
        DB 파일을 열고 스키마 생성 (이미 있으면 유지)
        Args:
            db_path: SQLite 파일 경로
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        """
        요청 단위 연결 컨텍스트

        블록이 정상 종료되면 커밋, 어떤 경우든 연결을 닫는다.
        Returns:
            sqlite3.Connection (row_factory=sqlite3.Row로 컬럼 이름 접근)
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- 시드 적재 ----------
    def load_seed(self, data_dir: Path = DATA) -> dict:
        """
        시드 데이터로 전 테이블을 비우고 다시 채움 (시연·평가 초기화용)

        원장 append-only 원칙의 예외. 시드 재적재 때만 지운다.
        Args:
            data_dir: 시드 루트 (r2_docs, r3_ledger, r4_governance 하위 폴더)
        Returns:
            테이블별 적재 건수 {"decisions", "policy_versions", "waivers", "documents"}
        """
        with self._conn() as c:
            c.execute("DELETE FROM decisions"); c.execute("DELETE FROM policy_rules")
            c.execute("DELETE FROM waivers"); c.execute("DELETE FROM documents")
        decisions = json.loads((data_dir / "r3_ledger" / "decisions_seed.json").read_text())["decisions"]
        for d in decisions:
            self._insert_decision(d)
        policies = json.loads((data_dir / "r4_governance" / "policies.json").read_text())["versions"]
        waivers = json.loads((data_dir / "r4_governance" / "waivers.json").read_text())["waivers"]
        docs = json.loads((data_dir / "r2_docs" / "manifest.json").read_text())["documents"]
        with self._conn() as c:
            for v in policies:
                # is_current·effective_from은 버전 단위 값이라 규칙 행마다 복사해 둔다
                for r in v["rules"]:
                    c.execute("INSERT INTO policy_rules VALUES (?,?,?,?,?,?,?,?,?)",
                              (v["policy_version"], r["rule_id"], int(v["is_current"]), v["effective_from"],
                               r["domain"], r["topic"], r["text"], r["approver_role"], r["threshold"]))
            for w in waivers:
                c.execute("INSERT INTO waivers VALUES (?,?,?,?,?,?,?,?)",
                          (w["waiver_id"], w["rule_id"], w["project_id"], w["scope"], w["approved_by"],
                           w["approved_at"], w["expires_at"], w["reason"]))
            for d in docs:
                c.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
                          (d["doc_id"], d["doc_version"], d["title"], d["file"], int(d["is_current"]),
                           d["updated_at"], d["change_note"]))
        return {"decisions": len(decisions), "policy_versions": len(policies),
                "waivers": len(waivers), "documents": len(docs)}

    def _insert_decision(self, d: dict) -> None:
        """
        결정 1건 INSERT
        Args:
            d: 결정 레코드. 정의된 컬럼 밖의 키는 extra(JSON)로 보관
        """
        # 스키마 컬럼에 없는 키(rejection_reason, r2_updates 등)는 버리지 않고 extra에 모은다
        known = {"decision_id", "project_id", "topic", "statement", "rationale", "alternatives_rejected",
                 "decided_by", "approved_by", "status", "effective_from", "superseded_by", "evidence"}
        extra = {k: v for k, v in d.items() if k not in known}
        with self._conn() as c:
            # 목록 컬럼은 JSON 문자열로 저장. ensure_ascii=False로 한글 인용을 그대로 보존
            c.execute(f"INSERT INTO decisions VALUES ({','.join('?' * len(DECISION_COLS))})",
                      (d["decision_id"], d["project_id"], d["topic"], d["statement"], d.get("rationale"),
                       json.dumps(d.get("alternatives_rejected", []), ensure_ascii=False),
                       d.get("decided_by"), d.get("approved_by"), d["status"], d.get("effective_from"),
                       d.get("superseded_by"), json.dumps(d.get("evidence", []), ensure_ascii=False),
                       json.dumps(extra, ensure_ascii=False)))

    # ---------- 결정 원장 ----------
    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> dict:
        """
        DB 행을 결정 dict로 복원
        Args:
            row: decisions 테이블 행
        Returns:
            JSON 컬럼을 풀고 extra를 펼친 뒤 is_active를 더한 dict
        """
        d = dict(row)
        d["alternatives_rejected"] = json.loads(d["alternatives_rejected"] or "[]")
        d["evidence"] = json.loads(d["evidence"] or "[]")
        d.update(json.loads(d.pop("extra") or "{}"))
        # 유효 결정 = 승인 + 대체 안 됨 (rag.ingest_decision의 active와 같은 규칙)
        d["is_active"] = d["status"] == "approved" and not d["superseded_by"]
        return d

    def get_decision(self, decision_id: str) -> dict | None:
        """
        결정 1건 조회
        Args:
            decision_id: 결정 ID
        Returns:
            결정 dict. 없으면 None
        """
        with self._conn() as c:
            row = c.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    def decisions(self, project_id: str | None = None, topic: str | None = None,
                  active_only: bool = False) -> list[dict]:
        """
        조건에 맞는 결정 목록 조회
        Args:
            project_id:  프로젝트로 한정. None이면 전체
            topic:       주제로 한정. None이면 전체
            active_only: True면 유효 결정만
        Returns:
            결정 dict 목록
        """
        # WHERE 1=1 뒤에 AND를 이어 붙여 조건 유무와 상관없이 같은 방식으로 SQL 조립
        sql, args = "SELECT * FROM decisions WHERE 1=1", []
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if topic:
            sql += " AND topic=?"; args.append(topic)
        with self._conn() as c:
            rows = [self._row_to_decision(r) for r in c.execute(sql, args).fetchall()]
        # is_active는 저장 컬럼이 아닌 계산값이라 SQL이 아니라 여기서 거른다
        return [r for r in rows if r["is_active"]] if active_only else rows

    def next_decision_id(self, project_id: str = "PRJ-OPSAI") -> str:
        """
        다음 결정 ID 발급

        프로젝트 약자 + 4자리 일련번호 (예: OPS-0011). 약자는 data/project/projects.json.
        Args:
            project_id: 결정이 속할 프로젝트 ID
        Returns:
            "{약자}-{번호:04d}" 형식의 새 결정 ID
        """
        key = project_key(project_id)
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT decision_id FROM decisions WHERE decision_id LIKE ?", (f"{key}-%",)).fetchall()]
        # 문자열 정렬이 아닌 숫자 최대값 + 1. 번호 자리가 숫자가 아닌 ID는 건너뛴다
        n = max((int(i.split("-")[1]) for i in ids if i.split("-")[1].isdigit()), default=0) + 1
        return f"{key}-{n:04d}"

    def propose(self, d: dict) -> str:
        """
        추출된 결정을 승인 대기(proposed)로 원장에 등록

        권위는 없다. 상태·승인자·대체 관계는 입력과 상관없이 초기값으로 덮어쓴다.
        Args:
            d: 결정 레코드 (decision_id가 없으면 새로 발급)
        Returns:
            등록된 결정 ID
        """
        d = dict(d)   # 호출 측 dict를 바꾸지 않도록 복사
        d.setdefault("decision_id", self.next_decision_id(d.get("project_id", "PRJ-OPSAI")))
        d["status"] = "proposed"; d["approved_by"] = None; d["superseded_by"] = None
        self._insert_decision(d)
        return d["decision_id"]

    def approve(self, decision_id: str, approved_by: str, effective_from: str | None = None) -> dict:
        """
        사람 승인

        같은 topic·프로젝트의 기존 유효 결정은 superseded로 바꾼다(삭제하지 않음).
        Args:
            decision_id:    승인할 결정 ID
            approved_by:    승인자
            effective_from: 시행일 (YYYY-MM-DD). None이면 오늘
        Returns:
            {"approved": 결정 ID, "superseded": 대체된 결정 ID 목록}
        Raises:
            KeyError: decision_id가 원장에 없을 때
        """
        d = self.get_decision(decision_id)
        if d is None:
            raise KeyError(decision_id)
        eff = effective_from or date.today().isoformat()
        # 대체 처리와 승인을 한 연결에서 처리해 한 번에 커밋
        with self._conn() as c:
            # 같은 프로젝트·주제의 유효 결정 중 자기 자신은 제외 (재승인 때 스스로를 대체하지 않게)
            prev = c.execute(
                "SELECT decision_id FROM decisions WHERE project_id=? AND topic=? AND status='approved' "
                "AND (superseded_by IS NULL OR superseded_by='') AND decision_id<>?",
                (d["project_id"], d["topic"], decision_id)).fetchall()
            for (pid,) in prev:
                c.execute("UPDATE decisions SET superseded_by=? WHERE decision_id=?", (decision_id, pid))
            c.execute("UPDATE decisions SET status='approved', approved_by=?, effective_from=? "
                      "WHERE decision_id=?", (approved_by, eff, decision_id))
        return {"approved": decision_id, "superseded": [p[0] for p in prev]}

    def reject(self, decision_id: str, rejected_by: str, reason: str = "") -> dict:
        """
        사람 반려

        원장에서 지우지 않고 상태만 rejected로 남긴다 (반려 이력도 근거).
        Args:
            decision_id: 반려할 결정 ID
            rejected_by: 반려한 사람
            reason:      반려 사유
        Returns:
            {"rejected": 결정 ID}
        """
        # 승인 대기 건만 반려 (이미 승인된 결정은 바뀌지 않음). 반려자·사유는 approved_by 칸에 기록
        with self._conn() as c:
            c.execute("UPDATE decisions SET status='rejected', approved_by=? WHERE decision_id=? AND status='proposed'",
                      (f"반려: {rejected_by} {reason}".strip(), decision_id))
        return {"rejected": decision_id}

    # ---------- 정책·예외 ----------
    def current_policy_version(self) -> str:
        """
        현행 정책 버전 조회
        Returns:
            is_current=1인 정책 버전 문자열
        """
        # 현행 버전은 하나라는 전제. 규칙 행이 여러 개라 LIMIT 1
        with self._conn() as c:
            return c.execute("SELECT policy_version FROM policy_rules WHERE is_current=1 LIMIT 1").fetchone()[0]

    def rule(self, rule_id: str, policy_version: str | None = None) -> dict | None:
        """
        정책 규칙 1건 조회
        Args:
            rule_id:        규칙 ID
            policy_version: 정책 버전. None이면 현행 버전
        Returns:
            규칙 dict. 없으면 None
        """
        ver = policy_version or self.current_policy_version()
        with self._conn() as c:
            row = c.execute("SELECT * FROM policy_rules WHERE rule_id=? AND policy_version=?",
                            (rule_id, ver)).fetchone()
        return dict(row) if row else None

    def rules(self, current_only: bool = True) -> list[dict]:
        """
        정책 규칙 목록 조회
        Args:
            current_only: True면 현행 버전 규칙만
        Returns:
            규칙 dict 목록
        """
        sql = "SELECT * FROM policy_rules" + (" WHERE is_current=1" if current_only else "")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def active_waivers(self, rule_id: str, project_id: str, on: str | None = None) -> list[dict]:
        """
        유효한 예외 조회

        만료 전·같은 프로젝트의 예외만 돌려준다. 예외는 정책 자체를 바꾸지 않는다.
        Args:
            rule_id:    정책 규칙 ID
            project_id: 대상 프로젝트 ID
            on:         기준 날짜 (YYYY-MM-DD). None이면 오늘
        Returns:
            예외 dict 목록
        """
        today = on or date.today().isoformat()
        # ISO 날짜 문자열이라 문자열 비교로 만료 판단. 만료일 당일까지 유효(>=)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM waivers WHERE rule_id=? AND project_id=? AND expires_at>=?",
                             (rule_id, project_id, today)).fetchall()
        return [dict(r) for r in rows]

    def all_waivers(self) -> list[dict]:
        """
        만료 여부와 상관없이 전체 예외 조회 (상태 지문 계산용)
        Returns:
            예외 dict 목록
        """
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM waivers").fetchall()]

    # ---------- 기준 문서 ----------
    def documents(self, current_only: bool = True) -> list[dict]:
        """
        기준 문서 목록 조회
        Args:
            current_only: True면 현행 버전만
        Returns:
            문서 dict 목록 (doc_id, doc_version, file, updated_at 등)
        """
        sql = "SELECT * FROM documents" + (" WHERE is_current=1" if current_only else "")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]
