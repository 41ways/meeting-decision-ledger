"""신뢰도 평가 러너 — 데이터셋(eval/reliability)을 임시 저장소에 적재하고 실제 파이프라인을 돌려 채점한다.

격리: 코드(agent·ledger·MCP 서버)와 data/project를 임시 루트로 복사하고 데이터셋 세계를 합친다.
코드가 경로를 ROOT/data/project로 고정해 두었기 때문이다. 원장·벡터 DB는 LEDGER_STORE 임시 폴더. 시연 store/는 건드리지 않는다.

시간순 재생: 회의를 날짜순으로 연다. 회의마다
  1) 그 회의 전사문을 R1에 넣고 MCP 서버를 다시 띄운다 (미래 회의가 검색에 섞이지 않게)
  2) 평가 대상이면 '회의 정리' 파이프라인을 돌린다
  3) 팀장이 정답대로 검토했다고 보고 결정을 원장에 반영한다 (승인·반려·대기, MCP 도구로)
  4) 회의 기록(미결·결정 후보)을 정답으로 다시 만든다
  5) 이 시점에 묻는 의견·질문을 돌린다
앞 단계의 오류가 뒤 채점에 번지지 않도록 원장은 항상 정답 기준이다 (항목별 독립 채점).
시험 셋 실행은 개발 셋 회의를 3)·4)만 재생한 뒤 시험 셋 회의부터 파이프라인을 돌린다.

사용: .venv/bin/python eval/reliability/run.py --split dev [--only meetings,opinions,questions] [--limit 3] [--resume] [--rescore]
결과: eval/reliability/results_{split}.md, results_{split}.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import dataset as D

ROOT = D.ROOT
REL = D.REL
CACHE = REL / ".cache"
CODE = ["agent", "ledger", "mcp_ledger_server.py"]
KINDS = ("meetings", "opinions", "questions")
OTHER_EVAL = r"run_eval|reliability/run.py|process_meetings|list_scaling"
NAME = {"value": ""}       # --name — 같은 분할을 코드·설정을 바꿔 여러 번 잴 때 결과 파일을 나눈다

# 임시 루트에서 회의 전사문 1건을 R1에 넣는 보조 스크립트 (MCP 서버가 꺼진 동안 별도 프로세스로 돈다 — 벡터 DB를 두 프로세스가 동시에 쓰지 않게)
INGEST_HELPER = '''import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from ledger.rag import LedgerRAG
from ledger.records import MeetingRecords
rag = LedgerRAG()
for mid in sys.argv[1:]:
    t = json.loads((ROOT / "data" / "project" / "r1_meetings" / f"{mid}.json").read_text(encoding="utf-8"))
    rag.ingest_transcript(t, t.get("domain", "meeting"))
MeetingRecords().sync_meetings()
'''


# ---------------------------------------------------------------- 통계
def wilson(k: int, n: int) -> tuple[float, float]:
    """
    비율의 95% 신뢰구간 (Wilson 점수 구간)

    표본이 작거나 비율이 0·100%에 가까워도 구간이 0~1 밖으로 나가지 않는다.
    Args:
        k: 맞은 수
        n: 전체 수
    Returns:
        (하한, 상한) 0~1. n이 0이면 (nan, nan)
    """
    if n == 0:
        return math.nan, math.nan
    z, p = 1.96, k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def rate(k: int, n: int) -> dict:
    """
    비율 지표 한 칸 (값·신뢰구간·표본 수)
    Args:
        k: 맞은 수
        n: 전체 수
    Returns:
        {"k", "n", "pct", "lo", "hi"} — pct·lo·hi는 백분율(소수 1자리), n이 0이면 None
    """
    lo, hi = wilson(k, n)
    f = (lambda x: round(100 * x, 1)) if n else (lambda x: None)
    return {"k": k, "n": n, "pct": f(k / n if n else 0), "lo": f(lo), "hi": f(hi)}


def pctl(xs: list[float], q: float) -> float | None:
    """
    백분위수 (가장 가까운 순위 방식)
    Args:
        xs: 값 목록
        q:  0~1 (0.5 = 중앙값, 0.95 = 상위 5%)
    Returns:
        백분위수. 값이 없으면 None
    """
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 1)


# ---------------------------------------------------------------- 임시 루트
def code_hash(tmp: Path) -> str:
    """
    기본 적재 캐시 키 — 세계·시연 데이터·적재 코드가 바뀌면 달라진다
    Args:
        tmp: 코드와 data/project를 펼친 임시 루트
    Returns:
        SHA-1 앞 12자리
    """
    h = hashlib.sha1()
    files = sorted([*(tmp / "data" / "project").rglob("*"), *D.WORLD.rglob("*"), *(tmp / "ledger").glob("*.py"),
                    tmp / "scripts" / "ingest.py"])
    for f in files:
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def make_root(rev: str | None = None) -> Path:
    """
    임시 루트를 만들고 기본 세계(시연 데이터 + 데이터셋 세계, 데이터셋 회의 없음)를 적재한다

    적재(임베딩)는 몇 분 걸려 결과를 .cache에 두고 같은 입력이면 재사용한다.
    rev를 주면 작업 트리 대신 그 커밋의 코드·시연 데이터로 돈다 (기준선을 나중 코드와 섞지 않고 다시 재려고).
    Args:
        rev: git 커밋 (없으면 작업 트리)
    Returns:
        임시 루트 경로 (LEDGER_STORE = 루트/store)
    Raises:
        subprocess.CalledProcessError: 적재 스크립트가 실패할 때
    """
    tmp = Path(tempfile.mkdtemp(prefix="ledger_rel_"))
    if rev:
        arc = subprocess.run(["git", "-C", str(ROOT), "archive", rev, *CODE, "scripts/ingest.py", "data/project"],
                             capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=arc, check=True)
    else:
        for c in CODE:
            src = ROOT / c
            (shutil.copytree if src.is_dir() else shutil.copy)(src, tmp / c)
        (tmp / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "ingest.py", tmp / "scripts" / "ingest.py")
    (tmp / "_rel_ingest.py").write_text(INGEST_HELPER, encoding="utf-8")
    D.build_data_dir(tmp / "data" / "project", [])
    env = {**os.environ, "LEDGER_STORE": str(tmp / "store")}
    cached = CACHE / f"base_{code_hash(tmp)}"
    if cached.exists():
        shutil.copytree(cached, tmp / "store")
    else:
        t = time.perf_counter()
        for attempt in range(3):
            if subprocess.run([sys.executable, str(tmp / "scripts" / "ingest.py")], env=env,
                              stdout=subprocess.DEVNULL).returncode == 0:
                break
            if attempt == 2:
                raise RuntimeError("기본 세계 적재 실패")
            unwedge()
        print(f"기본 세계 적재 {time.perf_counter() - t:.0f}초")
        CACHE.mkdir(exist_ok=True)
        shutil.copytree(tmp / "store", cached)
    os.environ["LEDGER_STORE"] = str(tmp / "store")
    return tmp


def unwedge() -> None:
    """
    Ollama 모델을 내린다 — 멈춘 요청이 8b를 붙잡고 있으면 임베딩 모델이 못 올라와 모든 검색이 시간 초과된다

    16GB 맥북에서는 8b와 임베딩 모델이 번갈아 올라간다. 내려도 다음 요청 때 다시 올라온다.
    """
    out = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout.splitlines()[1:]
    for line in out:
        if line.split():
            subprocess.run(["ollama", "stop", line.split()[0]], capture_output=True)


def other_evals() -> list[str]:
    """
    같은 맥북에서 도는 다른 평가 프로세스 (응답 시간 측정을 오염시킨다)
    Returns:
        'pid 명령' 줄 목록 (자기 자신 제외)
    """
    out = subprocess.run(["pgrep", "-fl", OTHER_EVAL], capture_output=True, text=True).stdout.splitlines()
    return [x for x in out if not x.startswith(f"{os.getpid()} ")]


# ---------------------------------------------------------------- 실행
class Runner:
    """
    시간순 재생기 — 임시 루트의 코드로 그래프와 MCP를 띄우고 회의·의견·질문을 돌린다
    """

    def __init__(self, root: Path, gold: dict, split: str, only: set[str], limit: int | None, raw: dict):
        """
        임시 루트 코드를 import하고 실행 상태를 준비한다
        Args:
            root:  make_root가 만든 임시 루트
            gold:  dataset.load_gold 결과
            split: dev / test
            only:  돌릴 항목 종류 (meetings·opinions·questions)
            limit: 분할 안 앞쪽 회의 수 제한 (빠른 확인용). None이면 전부
            raw:   이전 실행 결과 (--resume이면 이미 있는 항목은 다시 돌리지 않음)
        """
        sys.path.insert(0, str(root))
        # pylint: disable=import-outside-toplevel  # 임시 루트를 경로에 넣은 뒤에 읽어야 그쪽 data/project를 쓴다
        from agent.crosscheck import build_checkers
        from agent.ledger_graph import LedgerDeps, build_ledger_graph
        from ledger.llm import LocalLLM
        from ledger.mcp_bridge import LedgerMCP
        from ledger.records import MeetingRecords
        self.LedgerDeps, self.build, self.LedgerMCP = LedgerDeps, build_ledger_graph, LedgerMCP
        self.checkers = build_checkers()          # CROSSCHECK=1이면 작은 모델 2개가 8b 판단을 교차검증
        self.root, self.gold, self.split, self.only = root, gold, split, only
        self.records = MeetingRecords()
        self.llm = LocalLLM("qwen3:8b")
        self.mcp = None
        self.graph = self.deps = None
        self.led = D.Ledger()
        self.raw = raw
        mine = D.ordered(gold, split)
        self.run_meetings = set(mine[:limit] if limit else mine)
        last = max(self.run_meetings, key=lambda m: (gold["meetings"][m]["date"], m))
        self.timeline = [m for m in D.ordered(gold) if (gold["meetings"][m]["date"], m)
                         <= (gold["meetings"][last]["date"], last)]
        self.at = defaultdict(list)
        for x in gold["opinions"] + gold["questions"]:
            if x["split"] == split and x["at"] in self.run_meetings:
                self.at[x["at"]].append(x)
        self.applied: list[str] = []
        self.leads = {p["project_id"]: p["lead"] for p in json.loads(
            (root / "data" / "project" / "projects.json").read_text())["projects"]}

    async def restart(self, mid: str) -> None:
        """
        회의 하나를 연다 — MCP를 끄고 전사문을 R1에 넣은 뒤 다시 띄우고 그래프를 새로 만든다
        Args:
            mid: 여는 회의 ID
        Raises:
            subprocess.CalledProcessError: 적재 보조 스크립트가 실패할 때
        """
        if self.mcp:
            await self.mcp.stop()
        D.build_data_dir(self.root / "data" / "project", [mid])
        for attempt in range(3):          # 임베딩 시간 초과는 환경 문제라 다시 시도
            if subprocess.run([sys.executable, str(self.root / "_rel_ingest.py"), mid], env=os.environ).returncode == 0:
                break
            if attempt == 2:
                raise RuntimeError(f"{mid} 전사문 적재 실패")
            unwedge()
        self.mcp = self.LedgerMCP()
        await self.mcp.start()
        self.deps = self.LedgerDeps(fast_llm=self.llm, quality_llm=self.llm, mcp=self.mcp, checkers=self.checkers)
        self.graph = self.build(self.deps)

    async def go(self, msg: str, today: str, scope: str | None = None, refs: list | None = None) -> dict:
        """
        그래프 1회 실행 (시간·호출·토큰 측정, 예외는 결과에 남기고 계속)
        Args:
            msg:   입력 문장
            today: 기준 날짜 (예외 유효 여부 판단에 쓰임)
            scope: 화면에서 고른 프로젝트 약자
            refs:  직전 답이 가리킨 대상 ('이거' 이어 묻기)
        Returns:
            {"state", "sec", "calls", "tokens", "error"}
        """
        self.deps.today = today
        for attempt in range(3):
            t = time.perf_counter()
            try:
                st = await self.graph.ainvoke({"message": msg, "history": [], "events": [], "user_id": "",
                                               "scope_project": scope or "", "context_refs": refs or []})
                err = None
            except Exception as e:  # pylint: disable=broad-exception-caught  # 한 건 실패로 전체 측정을 멈추지 않는다
                st, err = {}, repr(e)
            # Ollama를 다른 프로세스와 나눠 쓰면 임베딩 모델이 늦게 올라와 시간 초과가 난다 — 환경 문제라 다시 돌린다
            if not (err and "imeout" in err) and not self.env_failed(st):
                break
            unwedge()
            await asyncio.sleep(5)
        u = st.get("usage") or {}
        xc = [{k: e.get(k) for k in ("kind", "target", "votes", "outcome")} for e in st.get("events", [])
              if e.get("type") == "CrossChecked"]
        return {"state": st, "sec": round(time.perf_counter() - t, 2), "calls": u.get("llm_calls", 0),
                "tokens": u.get("tokens", 0), "error": err, "xcheck": xc, "retries": attempt}

    @staticmethod
    def env_failed(st: dict) -> bool:
        """
        도구 호출이 환경 문제(시간 초과)로 실패한 흔적이 있는지 — 있으면 판정이 '조회 실패'로 바뀌어 측정이 오염된다
        Args:
            st: 그래프 최종 상태
        Returns:
            흔적이 있으면 True
        """
        items = (st.get("report") or {}).get("items") or []
        notes = [n for i in items for n in (i.get("review") or {}).get("notes", []) + (i.get("research") or {}).get("notes", [])]
        return any("조회 실패" in n or "imeout" in n for n in notes)

    async def call_ok(self, name: str, **args):
        """
        원장 반영용 MCP 호출 — 도구가 오류 문자열을 돌려주면(임베딩 시간 초과 등) 기다렸다 다시 부른다
        Args:
            name: 도구 이름
            **args: 도구 인자
        Returns:
            도구 결과 (dict)
        Raises:
            RuntimeError: 세 번 모두 실패할 때
        """
        for _ in range(3):
            r = await self.mcp.call(name, **args)
            if isinstance(r, dict):
                return r
            unwedge()
            await asyncio.sleep(5)
        raise RuntimeError(f"{name} 실패: {r}")

    async def apply_gold(self, mid: str) -> None:
        """
        팀장이 정답대로 검토했다고 보고 회의 결정을 원장에 반영한다 (MCP 도구 — 벡터 DB도 서버가 갱신)
        Args:
            mid: 회의 ID
        Raises:
            RuntimeError: 발급된 결정 번호가 시뮬레이션과 다를 때 (정답 참조가 어긋난다)
        """
        m = self.gold["meetings"][mid]
        pid = D.KEYS[m["project"]]
        lead = self.leads.get(pid, "팀장")
        segs = {s["seq"]: s["text"] for s in json.loads((D.MEETINGS / f"{mid}.json").read_text())["segments"]}
        before = dict(self.led.gid)
        self.led.apply(mid, m)
        for gd in sorted(m["decisions"], key=lambda x: x["seq"]):
            if gd["outcome"] == "confirms":
                await self.call_ok("approve_decision", decision_id=self.led.gid[gd["id"]], approved_by=lead,
                                    effective_from=m["date"])
                continue
            r = await self.call_ok("propose_decision", statement=gd["statement"], topic=gd["topic"],
                                    decided_by=gd["decided_by"], project_id=pid, rationale=gd.get("rationale", ""),
                                    alternatives_rejected=gd.get("alternatives_rejected", []),
                                    evidence=[{"meeting_id": mid, "seq": q, "quote": segs[q]} for q in gd["evidence_seq"]])
            if r.get("proposed") != self.led.gid[gd["id"]] or gd["id"] in before:
                raise RuntimeError(f"{gd['id']}: 원장 번호 {r.get('proposed')} ≠ 시뮬레이션 {self.led.gid[gd['id']]}")
            if gd["outcome"] == "approved":
                await self.call_ok("approve_decision", decision_id=r["proposed"], approved_by=lead,
                                    effective_from=m["date"])
            elif gd["outcome"] == "rejected":
                await self.call_ok("reject_decision", decision_id=r["proposed"], rejected_by=lead,
                                    reason="; ".join(c.get("detail", "") for c in gd.get("conflicts", [])))
        self.applied.append(mid)
        self.reset_records()

    def reset_records(self) -> None:
        """
        회의 기록(결정 후보·미결·할 일)을 지금까지 연 회의의 정답으로 다시 만든다

        파이프라인이 남긴 예측 기록(미결 연결 포함)을 지워 다음 질문이 정답 기록 위에서 돌게 한다. SQLite라 MCP와 동시에 써도 된다.
        """
        with sqlite3.connect(self.records.db_path) as c:
            c.execute("DELETE FROM items")
            c.execute("DELETE FROM threads")
            # 낱말 색인이 있는 코드면 미결 색인 줄도 지운다 — 아래 save_meeting이 정답 항목으로 다시 쓴다
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='term_index'").fetchone():
                c.execute("DELETE FROM term_index WHERE kind='O'")
        for mid in self.applied:
            m = self.gold["meetings"][mid]
            segs = {s["seq"]: s for s in json.loads((D.MEETINGS / f"{mid}.json").read_text())["segments"]}
            ev = lambda qs: [{"meeting_id": mid, "seq": q, "speaker": segs[q]["speaker"], "quote": segs[q]["text"]}  # noqa: E731
                             for q in qs if q in segs]
            items = [{"kind": "decision", "seq": gd["seq"], "statement": gd["statement"], "topic": gd["topic"],
                      "label": gd["expected_label"], "decision_id": self.led.gid.get(gd["id"]),
                      "evidence": ev(gd["evidence_seq"]), "state": "proposed"} for gd in m["decisions"]]
            items += [{"kind": "open", "seq": u["seq"], "statement": u["subject"], "topic": u["topic"], "label": "미결",
                       "evidence": ev(u["evidence_seq"]), "state": "open"} for u in m.get("undecided", [])]
            items += [{"kind": "action", "seq": a["seq"], "statement": a["statement"], "owner": a.get("owner"),
                       "due": a.get("due"), "label": "할 일", "evidence": ev(a["evidence_seq"]), "state": "open"}
                      for a in m["action_items"]]
            self.records.save_meeting(mid, None, items, "oracle", "oracle")

    async def meeting(self, mid: str) -> dict:
        """
        회의 정리 1건 실행과 결과 요약
        Args:
            mid: 회의 ID
        Returns:
            채점에 필요한 필드만 남긴 결과
        """
        r = await self.go(f"{mid} 회의 정리해줘", self.gold["meetings"][mid]["date"], mid.split("-")[0])
        rep = r.pop("state").get("report") or {}
        items = [{"seq": d.get("decided_seq"), "statement": d.get("statement"), "topic": d.get("topic"),
                  "decided_by": d.get("decided_by"), "label": d.get("label"), "label_reason": d.get("label_reason"),
                  "status": d["review"]["status"], "conflicts": d["review"]["conflicts"],
                  "changes": d["review"].get("changes", []), "confirms": d["review"].get("confirms", [])}
                 for d in rep.get("items", [])]
        acts = [{"seq": a.get("seq"), "statement": a.get("statement"), "owner": a.get("owner"), "due": a.get("due"),
                 "evidence": [e["seq"] for e in a.get("evidence", [])]} for a in rep.get("action_items", [])]
        und = [{"seq": u.get("seq"), "subject": u.get("subject"), "topic": u.get("topic")} for u in rep.get("undecided", [])]
        return {**r, "decisions": items, "action_items": acts, "undecided": und}

    async def opinion(self, o: dict, today: str) -> dict:
        """
        의견 검토 1건 실행 — 그래프 판정을 4종 verdict로 맞춘다 (eval/run_eval.py와 같은 규칙)
        Args:
            o:     의견 정답 항목
            today: 기준 날짜
        Returns:
            verdict·근거·라벨·시간 등
        """
        r = await self.go(o["text"], today, o.get("scope_project"))
        st = r.pop("state")
        its = (st.get("report") or {}).get("items") or []
        if not its or "review" not in its[0]:
            return {**r, "verdict": None, "mode": st.get("mode"), "against": []}
        it, rv = its[0], its[0]["review"]
        blocking = [c for c in rv["conflicts"] if c["type"] != "past_project"]
        verdict = ("allowed_with_waiver" if rv.get("waiver_applied") and not blocking else
                   "conflict" if rv["status"] == "conflict" else
                   "needs_approval" if rv["status"] == "needs_approval" else "no_conflict")
        return {**r, "verdict": verdict, "mode": st.get("mode"), "status": rv["status"],
                "against": [a for c in rv["conflicts"] for a in c["against"]] + rv.get("changes", []),
                "waiver": rv.get("waiver_applied"), "label": it.get("label"), "label_reason": it.get("label_reason"),
                "topic": it.get("topic"), "project": it.get("project")}

    async def question(self, q: dict, today: str, refs: list | None) -> dict:
        """
        질문 1건 실행
        Args:
            q:     질문 정답 항목
            today: 기준 날짜
            refs:  이어 묻기면 앞 질문 답이 가리킨 대상
        Returns:
            답·라벨·가리킨 대상·시간 등
        """
        r = await self.go(self.led.text(q["text"]), today, q.get("scope_project"), refs)
        st = r.pop("state")
        final = st.get("final") or ""
        lab = re.match(r"\[([^\]]+)\]", final)
        its = (st.get("report") or {}).get("items") or [{}]
        res = its[0].get("research") or {}
        reasons = list(its[0].get("check_reasons") or []) + [n for n in res.get("notes", []) if "주제" in n]
        return {**r, "final": final, "label": lab.group(1) if lab else None, "mode": st.get("mode"),
                "refs": (st.get("report") or {}).get("refs", []), "reasons": reasons,
                "authority": res.get("authority")}

    async def run(self) -> dict:
        """
        시간순 재생 전체
        Returns:
            {"meetings": {...}, "opinions": {...}, "questions": {...}, "id_map": {...}}
        """
        # 이어 돌리기: 오류로 끝난 항목은 다시 돈다 (잠자기·일시정지 뒤 연결이 끊긴 경우)
        out = {k: {i: v for i, v in self.raw.get(k, {}).items() if not v.get("error")} for k in KINDS}
        try:
            for n, mid in enumerate(self.timeline, 1):
                m = self.gold["meetings"][mid]
                await self.restart(mid)
                tag = "실행" if mid in self.run_meetings else "재생"
                if mid in self.run_meetings and "meetings" in self.only and mid not in out["meetings"]:
                    out["meetings"][mid] = await self.meeting(mid)
                    print(f"[{n}/{len(self.timeline)}] {mid} 회의 {out['meetings'][mid]['sec']}초", flush=True)
                await self.apply_gold(mid)
                for x in self.at.get(mid, []):
                    kind = "opinions" if "expected" in x else "questions"
                    if kind not in self.only or x["id"] in out[kind]:
                        continue
                    if kind == "opinions":
                        out[kind][x["id"]] = await self.opinion(x, m["date"])
                    else:
                        parent = out["questions"].get(x.get("follow_up_of") or "", {})
                        out[kind][x["id"]] = await self.question(x, m["date"], parent.get("refs"))
                    if out[kind][x["id"]].get("mode") == "meeting":
                        self.reset_records()       # 질문이 회의 정리로 잘못 가면 예측 기록이 남는다
                    save_raw(self.split, {**out, "id_map": self.led.gid})   # 일시정지·중단돼도 --resume으로 이어 가게 항목마다
                print(f"[{n}/{len(self.timeline)}] {mid} {tag} · 의견·질문 {len(self.at.get(mid, []))}건", flush=True)
                self.raw.update(out)
                save_raw(self.split, {**out, "id_map": self.led.gid})
        finally:
            if self.mcp:
                await self.mcp.stop()
        return {**out, "id_map": self.led.gid}


def tag(split: str) -> str:
    """
    결과 파일 이름표 — 교차검증을 켜면 따로 남겨 끈 기준선과 비교한다
    Args:
        split: dev / test
    Returns:
        'dev' 또는 'dev_crosscheck' 등
    """
    if NAME["value"]:
        return f"{split}_{NAME['value']}"
    return split + ("_crosscheck" if os.environ.get("CROSSCHECK", "0") == "1" else "")


def raw_path(split: str) -> Path:
    """
    실행 원자료 경로 (재채점·이어 돌리기용)
    Args:
        split: dev / test
    Returns:
        경로
    """
    return CACHE / f"raw_{tag(split)}.json"


def save_raw(split: str, raw: dict) -> None:
    """
    실행 원자료 저장 (회의 하나 끝날 때마다 — 중간에 멈춰도 --resume으로 이어 간다)
    Args:
        split: dev / test
        raw:   지금까지 결과
    """
    CACHE.mkdir(exist_ok=True)
    raw_path(split).write_text(json.dumps(raw, ensure_ascii=False, default=str), encoding="utf-8")


# ---------------------------------------------------------------- 채점
STATE_WORDS = {"미결": r"결정되지 않|미결|확정되지 않|정해지지 않|논의 중|논의만",
               "찾지 못함": r"찾지 못|결정되지 않|정해지지 않|정하지 않|논의된 적 없|관련 결정이 없|기록이 없"}
ANY_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3,4}-\d{2,4}(?!\d)")
PAUSE_SEC = 1800                                  # 이보다 긴 요청 시간은 일시정지가 섞인 것으로 보고 응답 시간 통계에서 뺀다
CLARIFY = "어떤 결정이나 회의를 말씀하시는지"       # 사실 조회가 대상을 되묻는 답 — 사람에게 되돌린 것으로 본다


def score_meetings(gold: dict, raw: dict, idm: dict) -> tuple[dict, list, dict]:
    """
    회의 정리 채점 — 결정·할 일·미결·함정·충돌·라벨, 자동 처리 판정

    결정 짝짓기: 예측의 확정 발언 번호가 정답 근거 발언(제안·확정)에 들면 같은 결정 (1:1, 확정 발언 일치 우선).
    Args:
        gold: load_gold 결과
        raw:  회의별 실행 결과
        idm:  정답 ID → 원장 ID
    Returns:
        (집계 카운터, 자동 처리 판정 목록, 현상별 카운터)
    """
    c = defaultdict(int)
    auto, tags = [], defaultdict(lambda: [0, 0])
    ref = lambda a: idm.get(a.strip("{}"), a.strip("{}"))  # noqa: E731
    for mid, pred in raw.items():
        g = gold["meetings"][mid]
        if pred.get("error"):
            c["error"] += 1
        preds = pred.get("decisions", [])
        used, match = set(), {}
        for gd in g["decisions"]:
            cands = [i for i, d in enumerate(preds) if i not in used and d["seq"] in gd["evidence_seq"]]
            cands.sort(key=lambda i: preds[i]["seq"] != gd["seq"])
            if cands:
                used.add(cands[0])
                match[gd["id"]] = preds[cands[0]]
        traps = {t["seq"]: t["kind"] for t in g.get("traps", [])}
        c["dec_gt"] += len(g["decisions"])
        c["dec_pred"] += len(preds)
        c["dec_hit"] += len(match)
        for i, d in enumerate(preds):
            if i not in used and d["seq"] in traps:
                c["trap_hit"] += 1
                tags[f"함정 오추출:{traps[d['seq']]}"][0] += 1
        c["traps"] += len(traps)
        for t in traps.values():
            tags[f"함정 오추출:{t}"][1] += 1
        for gd in g["decisions"]:
            d = match.get(gd["id"])
            for t in gd.get("tags", []):
                tags[f"결정 재현:{t}"][1] += 1
                tags[f"결정 재현:{t}"][0] += bool(d)
            blocking_exp = [x for x in gd.get("conflicts", []) if x["type"] in D.BLOCKING]
            rel_exp = [x for x in gd.get("conflicts", []) if x["type"] != "past_project"]
            got = set()
            if d:
                got = {a for x in d["conflicts"] if x["type"] != "past_project" for a in x["against"]} \
                    | set(d["changes"]) | set(d["confirms"])
                c["by_ok"] += d["decided_by"] == gd["decided_by"]
                c["topic_ok"] += d["topic"] == gd["topic"]
                c["topic_empty"] += not d["topic"]         # 정답 주제는 모두 목록 안 — 빈 주제(새 주제)로 둔 것
                c["label_ok"] += d["label"] == gd["expected_label"] or bool(gd.get("ambiguous"))
            for x in rel_exp:
                c["conf_gt"] += 1
                c["conf_hit"] += bool({ref(a) for a in x["against"]} & got)
                tags[f"충돌 탐지:{x['type']}"][1] += 1
                tags[f"충돌 탐지:{x['type']}"][0] += bool({ref(a) for a in x["against"]} & got)
            if d and not blocking_exp:
                c["fa_base"] += 1
                c["fa"] += any(x["type"] != "past_project" for x in d["conflicts"])
        gold_by_id = {gd["id"]: gd for gd in g["decisions"]}
        inv = {id(v): k for k, v in match.items()}
        for d in preds:
            gd = gold_by_id.get(inv.get(id(d)))
            ok_auto = bool(gd) and (gd["expected_label"] == "승인 대기" or bool(gd.get("ambiguous")))
            justified = not gd or gd["expected_label"] == "확인 필요" or bool(gd.get("ambiguous")) \
                or d["topic"] != gd["topic"]
            auto.append({"type": "결정", "mid": mid, "auto": d["label"] != "확인 필요", "ok": ok_auto,
                         "justified": justified})
        # 할 일
        attendees = json.loads((D.MEETINGS / f"{mid}.json").read_text(encoding="utf-8"))["attendees"]
        pas = pred.get("action_items", [])
        used = set()
        for ga in g["action_items"]:
            c["act_gt"] += 1
            words = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", ga["statement"]))
            pa = next((i for i, a in enumerate(pas) if i not in used and a["seq"] in ga["evidence_seq"]), None)
            if pa is None:
                pa = next((i for i, a in enumerate(pas) if i not in used
                           and words & set(re.findall(r"[가-힣A-Za-z0-9]{2,}", a["statement"] or ""))), None)
            if pa is None:
                for t in ga.get("tags", []):
                    tags[f"할 일 정확:{t}"][1] += 1
                continue
            used.add(pa)
            a = pas[pa]
            owner = re.sub(r"\s*\(.*\)", "", a["owner"]).strip() if a.get("owner") else None
            # 성을 뺀 이름('지수')은 참석자 한 명으로 정해지면 그 사람으로 본다
            full = [x for x in attendees if owner and x.endswith(owner) and len(owner) >= 2]
            owner = full[0] if len(full) == 1 else owner
            ok_o, ok_d = owner == ga.get("owner"), a.get("due") == ga.get("due")
            c["act_hit"] += 1
            c["act_owner"] += ok_o
            c["act_due"] += ok_d
            c["act_both"] += ok_o and ok_d
            for t in ga.get("tags", []):
                tags[f"할 일 정확:{t}"][1] += 1
                tags[f"할 일 정확:{t}"][0] += ok_o and ok_d
        # 미결
        pus = pred.get("undecided", [])
        c["und_gt"] += len(g.get("undecided", []))
        c["und_pred"] += len(pus)
        useq = {q for u in g.get("undecided", []) for q in u["evidence_seq"]}
        c["und_pred_ok"] += sum(u["seq"] in useq for u in pus)
        c["und_hit"] += sum(any(p["seq"] in u["evidence_seq"] for p in pus) for u in g.get("undecided", []))
    return c, auto, tags


def score_opinions(gold: dict, raw: dict, idm: dict) -> tuple[dict, list, dict]:
    """
    의견 검토 채점 — verdict 일치, 근거 일치, 자동 처리 판정
    Args:
        gold: load_gold 결과
        raw:  의견별 실행 결과
        idm:  정답 ID → 원장 ID
    Returns:
        (집계 카운터, 자동 처리 판정 목록, verdict별 카운터)
    """
    c, auto, by = defaultdict(int), [], defaultdict(lambda: [0, 0])
    g = {o["id"]: o for o in gold["opinions"]}
    for oid, p in raw.items():
        o = g[oid]
        ok = p.get("verdict") == o["expected"]
        c["n"] += 1
        c["ok"] += ok
        c["error"] += bool(p.get("error"))
        by[o["expected"]][1] += 1
        by[o["expected"]][0] += ok
        exp = {idm.get(a.strip("{}"), a.strip("{}")) for a in o.get("against", [])}
        if exp and o["expected"] != "no_conflict":
            c["basis_n"] += 1
            c["basis_ok"] += bool(exp & set(p.get("against", [])) or (o.get("waiver") and o["waiver"] == p.get("waiver")))
        # 의견을 질문 등 다른 경로로 보내 판정이 없으면 사람에게 넘긴 게 아니라 자동으로 틀린 답을 낸 것이다
        is_auto = p.get("verdict") != "needs_approval"
        c["misrouted"] += p.get("mode") not in (None, "opinion")
        auto.append({"type": "의견", "auto": is_auto, "ok": ok,
                     "justified": o["expected"] == "needs_approval" or bool(o.get("ambiguous"))})
    return c, auto, by


def answer_ok(q: dict, p: dict, idm: dict) -> bool:
    """
    질문 답 채점 — 기대 상태와 근거 ID·핵심어
    Args:
        q:   질문 정답 항목
        p:   실행 결과 (final, label)
        idm: 정답 ID → 원장 ID
    Returns:
        정답이면 True
    """
    final, label = p.get("final") or "", p.get("label")
    short = final.split("\n\n### ")[0]
    # 날짜는 '2026-12-07'과 '12월 7일'을 같은 값으로 본다 (핵심어 비교용으로 뒤에 덧붙임)
    short += " " + " ".join(f"{int(mo)}월 {int(dd)}일" for _, mo, dd in re.findall(r"(\d{4})-(\d{2})-(\d{2})", short))
    ids = [idm.get(a.strip("{}"), a.strip("{}")) for a in q.get("expected_ids", [])]
    kws = q.get("expected_keywords", [])
    if any(k in short for k in q.get("forbidden_keywords", [])):
        return False
    hit = any(i in short for i in ids if not re.fullmatch(r"[A-Z]{3}-M\d{2}-U\d+", i)) or bool(kws and all(k in short for k in kws))
    s = q["expected_state"]
    if s == "확정":
        return hit and label not in ("미결", "찾지 못함", "승인 대기")
    if s == "승인 대기":
        return ("승인 대기" in short or label == "승인 대기") and hit
    if s == "미결":
        return label == "미결" or (label != "확정" and bool(re.search(STATE_WORDS["미결"], short)))
    # 찾지 못함: 결정을 지어내지 않으면 정답 — 라벨이 틀려도 결정·조항 ID나 '승인 대기'를 대지 않고 '없다·정하지 않았다'고 답하면 맞다
    if label == "찾지 못함":
        return True
    return (not ANY_ID.search(short) and "승인 대기" not in short and label != "승인 대기"
            and bool(re.search(STATE_WORDS["찾지 못함"], short)))


def score_questions(gold: dict, raw: dict, idm: dict) -> tuple[dict, list, dict]:
    """
    질문 채점 — 상태별·종류별 정답률, 자동 처리 판정
    Args:
        gold: load_gold 결과
        raw:  질문별 실행 결과
        idm:  정답 ID → 원장 ID
    Returns:
        (집계 카운터, 자동 처리 판정 목록, 상태·종류별 카운터)
    """
    c, auto, by = defaultdict(int), [], defaultdict(lambda: [0, 0])
    g = {q["id"]: q for q in gold["questions"]}
    for qid, p in raw.items():
        q = g[qid]
        ok = answer_ok(q, p, idm)
        p["ok"] = ok
        c["n"] += 1
        c["ok"] += ok
        c["error"] += bool(p.get("error"))
        for k in (f"상태:{q['expected_state']}", f"종류:{q.get('kind', '?')}",
                  "화면 프로젝트 선택 없음" if not q.get("scope_project") else "화면 프로젝트 선택"):
            by[k][1] += 1
            by[k][0] += ok
        # 질문을 잡담 등 다른 경로로 보내 라벨 없이 답했으면 자동으로 낸 답이다. 되묻기만 사람에게 넘긴 것으로 센다
        is_auto = p.get("label") != "확인 필요" and not (p.get("final") or "").startswith(CLARIFY)
        c["misrouted"] += p.get("mode") not in (None, "question", "fact")
        auto.append({"type": "질문", "auto": is_auto, "ok": ok, "justified": not ok or bool(q.get("ambiguous"))})
    return c, auto, by


def boot_rate(pairs: list[tuple[int, int]], reps: int = 1000) -> dict:
    """
    회의 단위 부트스트랩 비율 — 한 회의의 결정끼리는 서로 닮아 독립 표본이 아니라서 회의를 뽑는 단위로 삼는다

    값은 회의별 개수를 합친 비율, 구간은 회의를 복원 추출한 1,000회 비율의 2.5·97.5 백분위.
    Args:
        pairs: 회의별 (맞은 수, 전체 수)
        reps:  반복 횟수
    Returns:
        rate와 같은 모양 + "ci": "boot"
    """
    k, n = sum(a for a, _ in pairs), sum(b for _, b in pairs)
    out = {**rate(k, n), "ci": "boot"}
    if not n:
        return out
    rng = random.Random(0)
    vals = []
    for _ in range(reps):
        smp = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        sk, sn = sum(a for a, _ in smp), sum(b for _, b in smp)
        if sn:
            vals.append(sk / sn)
    vals.sort()
    out["lo"], out["hi"] = round(100 * vals[int(0.025 * len(vals))], 1), round(100 * vals[int(0.975 * len(vals)) - 1], 1)
    return out


def reason_key(text: str | None) -> list[str]:
    """
    사람에게 넘긴 사유를 집계용 짧은 이름으로
    Args:
        text: 라벨 사유·확인 사유 원문
    Returns:
        사유 이름 목록 ('규정 충돌 · 권한 초과'는 둘로 나눈다)
    """
    if not text:
        return ["(사유 없음)"]
    parts = text.split(" · ") if all(len(x) < 12 for x in text.split(" · ")) else [text]
    return [re.sub(r"\d+(\.\d+)?", "N", re.split(r" — |\(|:", x)[0]).strip()[:30] for x in parts]


def handoff_reasons(raw: dict) -> dict:
    """
    사람에게 넘긴 사유 분포 (결정 라벨 사유, 의견 사유, 질문 확인 사유)
    Args:
        raw: 실행 원자료
    Returns:
        {"결정": {사유: 건수}, "의견": {...}, "질문": {...}}
    """
    out = {"결정": defaultdict(int), "의견": defaultdict(int), "질문": defaultdict(int)}
    for p in raw.get("meetings", {}).values():
        for d in p.get("decisions", []):
            if d.get("label") == "확인 필요":
                for r in reason_key(d.get("label_reason")):
                    out["결정"][r] += 1
    for p in raw.get("opinions", {}).values():
        if p.get("verdict") == "needs_approval":
            for r in reason_key(p.get("label_reason") or p.get("error")):
                out["의견"][r] += 1
    for p in raw.get("questions", {}).values():
        if p.get("label") != "확인 필요" and not (p.get("final") or "").startswith(CLARIFY):
            continue
        for r in reason_key((p.get("reasons") or [None])[0] if p.get("label") else "대상 되묻기"):
            out["질문"][r] += 1
    return {k: dict(sorted(v.items(), key=lambda x: -x[1])) for k, v in out.items() if v}


def xcheck_summary(gold: dict, raw: dict, idm: dict) -> dict:
    """
    교차검증(CROSSCHECK=1) 분석 — 작은 모델 이견이 실제 8b 오류를 짚었는가

    8b가 틀렸는지는 정답으로 판단한다. decision: 결정이라고 한 발언이 정답 결정 근거가 아님.
    relation: 같다·무관하다고 한 기준이 그 회의 정답 충돌·변경 근거에 있음. pick: 고른 결정이 질문 정답 ID가 아님.
    Args:
        gold: load_gold 결과
        raw:  실행 원자료 (항목별 xcheck 이벤트)
        idm:  정답 ID → 원장 ID
    Returns:
        {종류: {"이견(defer) 중 8b 오류", "재심 뒤 유지 중 8b 정확", "4/4 동의 중 8b 정확"}} — 이벤트가 없으면 빈 dict
    """
    ev = defaultdict(list)          # 종류 → (outcome, 4/4 동의, 8b 오류)
    for mid, p in raw.get("meetings", {}).items():
        g = gold["meetings"][mid]
        seqs = {q for gd in g["decisions"] for q in gd["evidence_seq"]}
        refs = {idm.get(a.strip("{}"), a.strip("{}")) for gd in g["decisions"] for c in gd.get("conflicts", [])
                if c["type"] in D.BLOCKING | {"change_approved"} for a in c["against"]}
        for x in p.get("xcheck", []):
            wrong = x["target"] not in seqs if x["kind"] == "decision" else x["target"] in refs
            ev[x["kind"]].append((x["outcome"], all(v is True for v in x.get("votes") or [None]), wrong))
    qg = {q["id"]: q for q in gold["questions"]}
    for qid, p in raw.get("questions", {}).items():
        q = qg[qid]
        ids = {idm.get(a.strip("{}"), a.strip("{}")) for a in q.get("expected_ids", [])}
        for x in p.get("xcheck", []):
            wrong = q["expected_state"] not in ("확정", "승인 대기") or x["target"] not in ids
            ev[x["kind"]].append((x["outcome"], all(v is True for v in x.get("votes") or [None]), wrong))
    if not ev:
        return {}
    ev["전체"] = [e for k in list(ev) for e in ev[k]]
    out = {}
    for k, xs in ev.items():
        d = [w for o, _, w in xs if o == "defer"]
        r = [not w for o, _, w in xs if o == "keep_after_rejudge"]
        a = [not w for o, al, w in xs if o == "keep" and al]
        out[k] = {"검증 건수": rate(len(xs), len(xs)), "이견으로 넘긴 것 중 8b 오류": rate(sum(d), len(d)),
                  "재심 뒤 유지한 것 중 8b 정확": rate(sum(r), len(r)), "4/4 동의일 때 8b 정확": rate(sum(a), len(a))}
    return out


def summarize(gold: dict, raw: dict) -> dict:
    """
    실행 원자료 전체를 지표로 집계 (신뢰구간 포함)
    Args:
        gold: load_gold 결과
        raw:  {"meetings", "opinions", "questions", "id_map"}
    Returns:
        {"metrics": {지표: rate 또는 값}, "breakdown": {...}, "cost": {...}}
    """
    idm = raw.get("id_map", {})
    mc, ma, mt = score_meetings(gold, raw.get("meetings", {}), idm)
    oc, oa, ob = score_opinions(gold, raw.get("opinions", {}), idm)
    qc, qa, qb = score_questions(gold, raw.get("questions", {}), idm)
    m = {}
    per = {mid: score_meetings(gold, {mid: p}, idm)[0] for mid, p in raw.get("meetings", {}).items()}

    def mb(num: str, den: str) -> dict:
        """회의 지표 — 회의 단위 부트스트랩 구간"""
        return boot_rate([(c[num], c[den]) for c in per.values()])
    rate_m = mb if len(per) > 1 else (lambda a, b: rate(mc[a], mc[b]))
    if raw.get("meetings"):
        m.update({
            "결정 재현율": rate_m("dec_hit", "dec_gt"), "결정 정밀도": rate_m("dec_hit", "dec_pred"),
            "결정자 정확(맞힌 결정 중)": rate_m("by_ok", "dec_hit"),
            "주제 정확(맞힌 결정 중)": rate_m("topic_ok", "dec_hit"),
            "새 주제로 둔 비율(정답 주제는 목록 안)": rate_m("topic_empty", "dec_hit"),
            "라벨 정확(맞힌 결정 중)": rate_m("label_ok", "dec_hit"),
            "함정 발언 오추출": rate_m("trap_hit", "traps"),
            "할 일 재현율": rate_m("act_hit", "act_gt"),
            "할 일 담당 정확": rate_m("act_owner", "act_gt"), "할 일 기한 정확": rate_m("act_due", "act_gt"),
            "할 일 담당·기한 모두 정확": rate_m("act_both", "act_gt"),
            "미결 재현율": rate_m("und_hit", "und_gt"), "미결 정밀도": rate_m("und_pred_ok", "und_pred"),
            "충돌·변경 탐지율": rate_m("conf_hit", "conf_gt"),
            "충돌 오탐(충돌 없는 결정 중)": rate_m("fa", "fa_base")})
    if raw.get("opinions"):
        m.update({"의견 판정 정확": rate(oc["ok"], oc["n"]), "의견 근거 일치": rate(oc["basis_ok"], oc["basis_n"]),
                  "의견 경로 오분류(의견으로 처리 안 함)": rate(oc["misrouted"], oc["n"])})
    if raw.get("questions"):
        m["질문 정답률"] = rate(qc["ok"], qc["n"])
        m["질문 경로 오분류(질의·사실 조회 아님)"] = rate(qc["misrouted"], qc["n"])
        nf = qb.get("상태:찾지 못함", [0, 0])
        m["'찾지 못함' 함정 정답률"] = rate(nf[0], nf[1])
    allauto = ma + oa + qa
    for name, xs in [("전체", allauto), ("결정", ma), ("의견", oa), ("질문", qa)]:
        if not xs:
            continue
        a = [x for x in xs if x["auto"]]
        h = [x for x in xs if not x["auto"]]
        if name == "결정" and len(per) > 1:      # 결정은 회의 단위로 묶어 구간을 낸다
            by = defaultdict(list)
            for x in xs:
                by[x["mid"]].append(x)
            grp = list(by.values())
            m["자동 처리율 · 결정"] = boot_rate([(sum(x["auto"] for x in g), len(g)) for g in grp])
            m["자동 처리 정확도 · 결정"] = boot_rate([(sum(x["ok"] for x in g if x["auto"]), sum(x["auto"] for x in g)) for g in grp])
            m["사람에게 넘긴 비율 · 결정"] = boot_rate([(sum(not x["auto"] for x in g), len(g)) for g in grp])
            m["넘긴 것 중 애매·오류 · 결정"] = boot_rate([(sum(x["justified"] for x in g if not x["auto"]),
                                                     sum(not x["auto"] for x in g)) for g in grp])
            continue
        m[f"자동 처리율 · {name}"] = rate(len(a), len(xs))
        m[f"자동 처리 정확도 · {name}"] = rate(sum(x["ok"] for x in a), len(a))
        m[f"사람에게 넘긴 비율 · {name}"] = rate(len(h), len(xs))
        m[f"넘긴 것 중 애매·오류 · {name}"] = rate(sum(x["justified"] for x in h), len(h))
    cost = {}
    for k in KINDS:
        xs = list(raw.get(k, {}).values())
        if xs:
            # 배터리 감시가 측정을 일시정지(SIGSTOP)하면 그 요청 시간에 멈춘 시간이 섞인다 — 30분 넘는 건 빼고 따로 적는다
            paused = [x for x in xs if x["sec"] > PAUSE_SEC]
            secs = [x["sec"] for x in xs if x["sec"] <= PAUSE_SEC]
            cost[k] = {"n": len(xs), "median_sec": pctl(secs, 0.5), "p95_sec": pctl(secs, 0.95),
                       "llm_calls": sum(x["calls"] for x in xs), "tokens": sum(x["tokens"] for x in xs),
                       "calls_per_item": round(sum(x["calls"] for x in xs) / len(xs), 1),
                       "tokens_per_item": round(sum(x["tokens"] for x in xs) / len(xs)),
                       "errors": sum(bool(x.get("error")) for x in xs), "paused_excluded": len(paused)}
    bd = {"회의 현상별": {k: rate(*v) for k, v in sorted(mt.items())},
          "의견 기대 판정별": {k: rate(*v) for k, v in sorted(ob.items())},
          "질문 상태·종류별": {k: rate(*v) for k, v in sorted(qb.items())}}
    return {"metrics": m, "breakdown": bd, "cost": cost, "handoff_reasons": handoff_reasons(raw),
            "crosscheck": xcheck_summary(gold, raw, idm)}


def fmt(r) -> str:
    """
    지표 한 칸 표기 — '82.0% (73.4–88.3) · 82/100'
    Args:
        r: rate 결과
    Returns:
        표 칸 문자열
    """
    if r["n"] == 0:
        return "-"
    mark = "†" if r.get("ci") == "boot" else ""
    return f"{r['pct']}% ({r['lo']}–{r['hi']}){mark} · {r['k']}/{r['n']}"


def to_md(split: str, s: dict, meta: dict) -> str:
    """
    결과 Markdown
    Args:
        split: dev / test
        s:     summarize 결과
        meta:  실행 정보 (커밋, 시간, 다른 평가 동시 실행 여부)
    Returns:
        Markdown 문자열
    """
    rows = [f"# 신뢰도 평가 결과 — {'개발' if split == 'dev' else '시험'} 셋", "",
            f"- 실행: {meta['run_at']} · 코드 {meta['commit']} · 모델 {meta.get('model', 'qwen3:8b')} "
            f"(백엔드 {meta.get('backend', 'ollama')}, temperature 0) · 교차검증 {'켬' if meta.get('crosscheck') else '끔'}",
            f"- 규모: 회의 {meta['n']['meetings']} · 의견 {meta['n']['opinions']} · 질문 {meta['n']['questions']}",
            f"- 걸린 시간: {meta['wall_min']}분" + (f" · 측정 중 다른 평가 동시 실행: {meta['others']}" if meta.get("others") else ""),
            "- 신뢰구간 95%: 회의에서 나온 지표(결정·할 일·미결·충돌)는 회의 단위 부트스트랩 1,000회(†), "
            "의견·질문과 전체 합계는 Wilson 점수 구간.",
            "- 원장은 매 시점 정답 기준(팀장이 정답대로 검토했다고 가정) — 항목별 독립 채점.", "",
            "## 핵심 지표", "", "| 지표 | 값 (95% 신뢰구간) · 표본 |", "|---|---|"]
    key = [k for k in s["metrics"] if k.startswith(("자동 처리", "사람에게", "넘긴 것"))]
    rows += [f"| {k} | {fmt(s['metrics'][k])} |" for k in key if k.endswith("전체")]
    rows += ["", "## 항목별 정확도", "", "| 지표 | 값 (95% 신뢰구간) · 표본 |", "|---|---|"]
    rows += [f"| {k} | {fmt(v)} |" for k, v in s["metrics"].items() if k not in key]
    rows += ["", "## 자동 처리 · 사람 확인 (종류별)", "", "| 지표 | 값 (95% 신뢰구간) · 표본 |", "|---|---|"]
    rows += [f"| {k} | {fmt(s['metrics'][k])} |" for k in key if not k.endswith("전체")]
    rows += ["", "## 비용", "", "| 종류 | 건수 | 응답 시간 중앙값(초) | 상위 5%(초) | LLM 호출(건당) | 토큰(건당) | 오류 | 일시정지로 시간 제외 |",
             "|---|---|---|---|---|---|---|---|"]
    for k, v in s["cost"].items():
        rows.append(f"| {k} | {v['n']} | {v['median_sec']} | {v['p95_sec']} | {v['llm_calls']} ({v['calls_per_item']}) "
                    f"| {v['tokens']} ({v['tokens_per_item']}) | {v['errors']} | {v.get('paused_excluded', 0)} |")
    if s.get("handoff_reasons"):
        rows += ["", "## 사람에게 넘긴 사유", "", "| 종류 | 사유 | 건수 |", "|---|---|---|"]
        rows += [f"| {k} | {r} | {n} |" for k, tab in s["handoff_reasons"].items() for r, n in tab.items()]
    if s.get("crosscheck"):
        rows += ["", "## 교차검증 (작은 모델 2개)", "",
                 "| 검증 대상 | 건수 | 이견으로 넘긴 것 중 8b 오류 | 재심 뒤 유지한 것 중 8b 정확 | 4/4 동의일 때 8b 정확 |",
                 "|---|---|---|---|---|"]
        rows += [f"| {k} | {v['검증 건수']['n']} | {fmt(v['이견으로 넘긴 것 중 8b 오류'])} | "
                 f"{fmt(v['재심 뒤 유지한 것 중 8b 정확'])} | {fmt(v['4/4 동의일 때 8b 정확'])} |"
                 for k, v in s["crosscheck"].items()]
    for name, tab in s["breakdown"].items():
        if tab:
            rows += ["", f"## {name}", "", "| 구분 | 값 (95% 신뢰구간) · 표본 |", "|---|---|"]
            rows += [f"| {k} | {fmt(v)} |" for k, v in tab.items()]
    rows += ["", "## 지표 정의", "",
             "- 자동 처리: 결정은 라벨이 '확인 필요'가 아님, 의견은 판정이 needs_approval이 아님(다른 경로로 가 판정이 없으면 자동 처리한 오답), 질문은 답 라벨이 '확인 필요'가 아니고 대상을 되묻지 않음(잡담 경로로 간 답도 자동 처리).",
             "- 질문 정답: 확정은 결정 ID나 핵심어 전부, 승인 대기는 '승인 대기'와 ID, 미결은 미결 라벨이나 '결정되지 않음' 표현, 찾지 못함은 결정·조항 ID와 '승인 대기'를 대지 않고 '찾지 못함·정하지 않음'으로 답함(라벨이 틀려도 내용으로 채점). 금지어(지난 값)가 있으면 오답.",
             "- 할 일 담당: 괄호 설명을 떼고, 성을 뺀 이름은 참석자 한 명으로 정해지면 같은 사람으로 본다.",
             "- 자동 처리 정확도: 결정은 정답 결정이면서 기대 라벨이 '승인 대기'(충돌 없음), 의견은 판정 일치, 질문은 정답.",
             "- 넘긴 것 중 애매·오류: 결정은 정답에 없는 결정이거나 기대 라벨이 '확인 필요'이거나 애매 표시이거나 주제를 잘못 고름, "
             "의견은 기대 판정이 needs_approval이거나 애매 표시, 질문은 답이 틀렸거나 애매 표시.",
             "- 충돌·변경 탐지율: 정답의 막는 충돌·기준 변경·승인 대기 확정 관계 중 예측이 같은 근거 ID로 짚은 비율 (결정을 놓치면 탐지 실패)."]
    return "\n".join(rows) + "\n"


async def amain(args) -> None:
    """
    실행·채점·저장
    Args:
        args: 명령행 인자
    """
    NAME["value"] = args.name or ""
    gold = D.load_gold()
    err, _, _ = D.check(gold)
    if args.limit:          # 작성 중 빠른 확인 — 규모 목표 미달은 넘어간다
        err = [e for e in err if "(목표" not in e]
    if err:
        print("데이터셋 검사 오류 — 먼저 고친다:\n  " + "\n  ".join(err[:20]))
        sys.exit(1)
    only = set(args.only.split(",")) if args.only else set(KINDS)
    raw = json.loads(raw_path(args.split).read_text()) if (args.resume or args.rescore) and raw_path(args.split).exists() else {}
    t0 = time.perf_counter()
    others = other_evals()
    if others and not args.rescore:
        print("주의: 다른 평가가 돌고 있다 — 응답 시간이 오염된다\n  " + "\n  ".join(others))
    if not args.rescore:
        root = make_root(args.rev)
        try:
            raw = await Runner(root, gold, args.split, only, args.limit, raw).run()
        finally:
            if not args.keep:
                shutil.rmtree(root, ignore_errors=True)
            else:
                print("임시 루트:", root)
    s = summarize(gold, raw)
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", args.rev or "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    meta = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "commit": commit,
            "wall_min": round((time.perf_counter() - t0) / 60, 1), "others": "; ".join(others),
            "n": {k: len(raw.get(k, {})) for k in KINDS}, "model": "qwen3:8b",
            "backend": os.environ.get("LLM_BACKEND", "ollama"), "crosscheck": os.environ.get("CROSSCHECK", "0") == "1",
            "crosscheck_models": os.environ.get("CROSSCHECK_MODELS", "qwen3:0.6b,qwen2.5:0.5b")
            if os.environ.get("CROSSCHECK", "0") == "1" else None}
    if args.rescore and (REL / f"results_{tag(args.split)}.json").exists():
        meta.update({k: v for k, v in json.loads((REL / f"results_{tag(args.split)}.json").read_text())["meta"].items()
                     if k in ("run_at", "wall_min", "others", "commit")})
    (REL / f"results_{tag(args.split)}.json").write_text(json.dumps({"meta": meta, **s, "raw": raw}, ensure_ascii=False,
                                                              indent=1, default=str), encoding="utf-8")
    md = to_md(args.split, s, meta)
    (REL / f"results_{tag(args.split)}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument("--only", help="meetings,opinions,questions 중 일부 (쉼표). 없으면 전부")
    ap.add_argument("--limit", type=int, help="분할 안 앞쪽 회의 N건까지만 (빠른 확인용)")
    ap.add_argument("--resume", action="store_true", help="이전 원자료(.cache/raw_<split>.json)에 있는 항목은 건너뜀")
    ap.add_argument("--rescore", action="store_true", help="실행 없이 원자료로 다시 채점")
    ap.add_argument("--keep", action="store_true", help="임시 루트를 지우지 않음 (디버깅)")
    ap.add_argument("--rev", help="이 커밋의 코드·시연 데이터로 실행 (없으면 작업 트리)")
    ap.add_argument("--name", help="결과 파일 이름표 (results_<split>_<name>). 없으면 교차검증 여부로 정함")
    asyncio.run(amain(ap.parse_args()))
