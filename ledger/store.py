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

DECISION_COLS = ["decision_id", "project_id", "topic", "statement", "rationale",
                 "alternatives_rejected", "decided_by", "approved_by", "status",
                 "effective_from", "superseded_by", "evidence", "extra"]


class LedgerStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- 시드 적재 ----------
    def load_seed(self, data_dir: Path = DATA) -> dict:
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
        known = {"decision_id", "project_id", "topic", "statement", "rationale", "alternatives_rejected",
                 "decided_by", "approved_by", "status", "effective_from", "superseded_by", "evidence"}
        extra = {k: v for k, v in d.items() if k not in known}
        with self._conn() as c:
            c.execute(f"INSERT INTO decisions VALUES ({','.join('?' * len(DECISION_COLS))})",
                      (d["decision_id"], d["project_id"], d["topic"], d["statement"], d.get("rationale"),
                       json.dumps(d.get("alternatives_rejected", []), ensure_ascii=False),
                       d.get("decided_by"), d.get("approved_by"), d["status"], d.get("effective_from"),
                       d.get("superseded_by"), json.dumps(d.get("evidence", []), ensure_ascii=False),
                       json.dumps(extra, ensure_ascii=False)))

    # ---------- 결정 원장 ----------
    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["alternatives_rejected"] = json.loads(d["alternatives_rejected"] or "[]")
        d["evidence"] = json.loads(d["evidence"] or "[]")
        d.update(json.loads(d.pop("extra") or "{}"))
        d["is_active"] = d["status"] == "approved" and not d["superseded_by"]
        return d

    def get_decision(self, decision_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    def decisions(self, project_id: str | None = None, topic: str | None = None,
                  active_only: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM decisions WHERE 1=1", []
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if topic:
            sql += " AND topic=?"; args.append(topic)
        with self._conn() as c:
            rows = [self._row_to_decision(r) for r in c.execute(sql, args).fetchall()]
        return [r for r in rows if r["is_active"]] if active_only else rows

    def next_decision_id(self, project_id: str = "PRJ-OPSAI") -> str:
        """프로젝트 약자 + 4자리 일련번호 (예: OPS-0011). 약자는 data/project/projects.json."""
        key = project_key(project_id)
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT decision_id FROM decisions WHERE decision_id LIKE ?", (f"{key}-%",)).fetchall()]
        n = max((int(i.split("-")[1]) for i in ids if i.split("-")[1].isdigit()), default=0) + 1
        return f"{key}-{n:04d}"

    def propose(self, d: dict) -> str:
        """추출된 결정을 승인 대기(proposed)로 원장에 올린다. 권위는 없다."""
        d = dict(d)
        d.setdefault("decision_id", self.next_decision_id(d.get("project_id", "PRJ-OPSAI")))
        d["status"] = "proposed"; d["approved_by"] = None; d["superseded_by"] = None
        self._insert_decision(d)
        return d["decision_id"]

    def approve(self, decision_id: str, approved_by: str, effective_from: str | None = None) -> dict:
        """사람 승인. 같은 topic·프로젝트의 기존 유효 결정은 superseded로 바꾼다(삭제하지 않음)."""
        d = self.get_decision(decision_id)
        if d is None:
            raise KeyError(decision_id)
        eff = effective_from or date.today().isoformat()
        with self._conn() as c:
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
        """사람 반려. 원장에서 지우지 않고 상태만 rejected로 남긴다 (반려 이력도 근거)."""
        with self._conn() as c:
            c.execute("UPDATE decisions SET status='rejected', approved_by=? WHERE decision_id=? AND status='proposed'",
                      (f"반려: {rejected_by} {reason}".strip(), decision_id))
        return {"rejected": decision_id}

    # ---------- 정책·예외 ----------
    def current_policy_version(self) -> str:
        with self._conn() as c:
            return c.execute("SELECT policy_version FROM policy_rules WHERE is_current=1 LIMIT 1").fetchone()[0]

    def rule(self, rule_id: str, policy_version: str | None = None) -> dict | None:
        ver = policy_version or self.current_policy_version()
        with self._conn() as c:
            row = c.execute("SELECT * FROM policy_rules WHERE rule_id=? AND policy_version=?",
                            (rule_id, ver)).fetchone()
        return dict(row) if row else None

    def rules(self, current_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM policy_rules" + (" WHERE is_current=1" if current_only else "")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def active_waivers(self, rule_id: str, project_id: str, on: str | None = None) -> list[dict]:
        """만료 전·같은 프로젝트의 예외만 돌려준다. 예외는 정책 자체를 바꾸지 않는다."""
        today = on or date.today().isoformat()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM waivers WHERE rule_id=? AND project_id=? AND expires_at>=?",
                             (rule_id, project_id, today)).fetchall()
        return [dict(r) for r in rows]

    def all_waivers(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM waivers").fetchall()]

    # ---------- 기준 문서 ----------
    def documents(self, current_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM documents" + (" WHERE is_current=1" if current_only else "")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]
