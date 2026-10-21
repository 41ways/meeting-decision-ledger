"""목록 크기 곡선 — LLM이 읽는 결정 목록이 길어지면 정답 ID를 고르는 정확도·시간·토큰이 어떻게 변하나.

같은 질문 집합(ground_truth 근거 질의 + 간단한 질문 20개)을 두 방식으로 돌린다.
  전체 읽기      : 원장 전체 목록(진짜 결정 11건 + 가짜 결정)을 한 번에 LLM에 준다 (예전 방식을 끝까지 키운 것)
  나눠 읽기      : 같은 전체 목록을 LIST_TOKEN_BUDGET 단위로 나눠 읽고 합친다 (전체 읽기 대비책의 읽는 방식)
  좁힌 뒤 읽기   : agent/retrieve.pick 그대로 — 별칭·ID 색인으로 좁힌 목록만 읽는다 (색인 0건이면 그 안에서 전체 읽기 대비책)
목록 크기 50 · 100 · 200 · 400건. 진짜 결정이 모자라 가짜 결정을 섞는다 — 대부분 다른 사안, 15%는 같은 주제의 다른 사안.
가짜 결정은 정답이 아니므로 골랐다면 오선택.

채점
  정답: 기대 결정 ID를 골랐는가 (기대가 '없음'이면 결정을 하나도 고르지 않았는가)
  오선택: 기대 밖의 결정 ID를 고른 수

사용: .venv/bin/python eval/list_scaling.py [--sizes 50,100,200,400] [--force]
결과: eval/list_scaling.json, eval/list_scaling.md
시간을 재므로 다른 평가(run_eval·reliability·process_meetings)가 돌고 있으면 시작하지 않는다 (--force로 무시).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import retrieve as R  # noqa: E402
from ledger.llm import LocalLLM  # noqa: E402
from ledger.records import MeetingRecords  # noqa: E402
from ledger.store import LedgerStore  # noqa: E402
from ledger.term_index import find_candidates, project_records  # noqa: E402

GT = json.loads((ROOT / "eval" / "ground_truth.json").read_text())

# (질문, 프로젝트, 기대 결정 ID 목록 — 빈 목록이면 '해당 결정 없음')
EXTRA = [
    ("백엔드 프레임워크는 뭘로 정했어?", "PRJ-OPSAI", ["OPS-0001"]),
    ("LangGraph로 Agent 흐름 짜기로 한 거 맞지?", "PRJ-OPSAI", ["OPS-0001"]),
    ("MCP 서버는 어떻게 띄우기로 했어?", "PRJ-OPSAI", ["OPS-0002"]),
    ("MCP를 stdio로 붙이기로 한 이유는?", "PRJ-OPSAI", ["OPS-0002"]),
    ("외부 LLM API 써도 돼?", "PRJ-OPSAI", ["OPS-0004"]),
    ("기본 LLM은 뭘 쓰기로 했어?", "PRJ-OPSAI", ["OPS-0004"]),
    ("최종 답변은 몇 초 안에 나와야 해?", "PRJ-OPSAI", ["OPS-0005"]),
    ("응답 시간 기준 바꾼 결정 뭐야?", "PRJ-OPSAI", ["OPS-0005"]),
    ("Supervisor 구조로 하기로 한 결정은?", "PRJ-OPSAI", ["OPS-0006"]),
    ("하위 에이전트에 위임하는 구조는 누가 정했어?", "PRJ-OPSAI", ["OPS-0006"]),
    ("웹소켓 말고 SSE 쓰는 이유가 뭐야?", "PRJ-OPSAI", ["OPS-0007"]),
    ("대화 이력은 어디에 저장해?", "PRJ-OPSAI", ["OPS-0008"]),
    ("질문을 simple/complex로 나누기로 했어?", "PRJ-OPSAI", ["OPS-0009"]),
    ("라우팅은 어디서 하기로 했어?", "PRJ-OPSAI", ["OPS-0009"]),
    ("분류는 작은 모델로 하기로 했어?", "PRJ-OPSAI", ["OPS-0010"]),
    ("고객센터 챗봇은 대화 이력을 어디에 저장했어?", "PRJ-CSBOT", ["CSB-0005"]),
    ("고객센터 챗봇 상담원 알림은 결국 뭘로 했어?", "PRJ-CSBOT", ["CSB-0004"]),
    ("고객센터 챗봇에서 MySQL로 옮긴 결정은?", "PRJ-CSBOT", ["CSB-0005"]),
    ("메신저를 팀즈로 바꾸기로 했나요?", "PRJ-OPSAI", []),
    ("회식 장소는 어디로 정했어?", "PRJ-OPSAI", []),
]


def questions() -> list[tuple[str, str, list[str]]]:
    """
    질문 집합 — ground_truth 근거 질의 6건 + 위의 20건
    Returns:
        [(질문, 프로젝트 ID, 기대 결정 ID 목록)]
    """
    out = []
    for q in GT["questions"]:
        pid = "PRJ-CSBOT" if "고객센터" in q["text"] else "PRJ-OPSAI"
        # 미결(Q3)은 원장 결정이 아니라서 '결정 없음'이 정답
        out.append((q["text"], pid, [q["expected_decision"]] if q.get("expected_decision") else []))
    return out + EXTRA


# 다른 사안의 가짜 결정: 주제 40개 × 방식 10개. 실제 주제의 별칭과 겹치지 않는 말만 쓴다
OTHER_SUBJECTS = [
    "알림 채널", "장애 등급 기준", "로그 보관 위치", "대시보드 갱신 주기", "배포 요일", "코드 리뷰 인원", "브랜치 전략",
    "테스트 커버리지 목표", "문서 작성 도구", "회의록 양식", "온콜 순번", "야간 장애 연락", "접근 권한 신청",
    "화면 글꼴", "다크 모드", "다국어 지원", "감사 로그 항목", "백업 주기", "모니터링 지표", "경보 임계치",
    "런북 작성 담당", "프롬프트 버전 관리", "평가 데이터셋 규모", "피드백 수집 창구", "장애 보고서 양식",
    "용어집 관리자", "권한 등급 이름", "화면 색상 테마", "질문 예시 개수", "첨부 파일 크기 제한", "검색 결과 개수",
    "답변 길이 상한", "로그인 방식", "데이터 마스킹 대상", "운영 매뉴얼 위치", "신규 입사자 안내", "개발 서버 사양",
    "장애 회고 주기", "사용자 만족도 조사", "화면 접근성 점검"]
WAYS = ["팀장 승인 후 적용", "분기마다 재검토", "운영팀 요청 기준", "주 1회 점검", "PL 관리", "공지 후 적용",
        "월말 일괄 반영", "시범 운영 뒤 확정", "현업 의견 반영", "담당자 지정 후 운영"]
# 같은 주제의 다른 사안 — 색인으로 좁혀도 함께 걸리는 가짜 (별칭 충돌을 흉내 낸다)
SAME_TOPIC = {
    "realtime.transport": ["SSE 재연결 간격은 3초로 한다", "진행 표시 문구는 단계 이름으로 보여 준다",
                           "실시간 전송 이벤트 이름은 영어 소문자로 쓴다", "웹소켓 시험 서버는 개발망에만 둔다",
                           "롱폴링 요청 제한 시간은 25초로 한다", "실시간 전송 로그는 7일만 남긴다"],
    "session.store": ["대화 이력 화면은 최근 20개만 보여 준다", "대화 저장 테이블 이름은 chat_history로 한다",
                      "대화 이력 내보내기는 CSV로 한다", "PostgreSQL 접속 풀은 10개로 한다",
                      "대화 기억은 사용자별로 분리한다", "대화 이력 검색은 제목만 대상으로 한다"],
    "perf.latency": ["응답 시간 측정은 서버 기준으로 한다", "응답 시간 대시보드는 매일 갱신한다",
                     "지연 시간 경보는 운영팀에 보낸다", "응답 속도 시험은 주 1회 돌린다",
                     "응답 시간 표기는 초 단위로 한다", "첫 진행 표시 문구는 '확인 중'으로 한다"],
    "llm.external": ["외부 LLM 비교 결과는 위키에 올린다", "사내 모델 목록은 PL이 관리한다",
                     "외부 API 키는 보안팀이 보관한다", "Ollama 서버 재시작은 야간에 한다",
                     "사내 LLM 응답은 로그에 남긴다", "외부 모델 시험 계정은 1개만 만든다"],
    "tech.backend": ["FastAPI 문서 화면은 개발망에서만 연다", "LangGraph 그래프 그림은 문서에 넣는다",
                     "백엔드 프레임워크 버전은 분기마다 올린다", "FastAPI 라우터는 기능별로 나눈다",
                     "기술 스택 표는 README에 둔다", "LangGraph 노드 이름은 동사로 쓴다"],
    "mcp.deployment": ["MCP 도구 이름은 동사로 시작한다", "MCP 서버 로그는 파일로 남긴다",
                       "MCP 도구 설명은 한국어로 쓴다", "자식 프로세스 재시작은 3번까지 한다",
                       "MCP 점검 스크립트는 배포 전에 돌린다", "stdio 통신 오류는 운영팀에 알린다"],
    "agent.structure": ["Supervisor 프롬프트는 버전 번호를 붙인다", "하위 Agent 이름은 역할로 짓는다",
                        "에이전트 구조 그림은 설계서에 넣는다", "하위 에이전트 로그는 따로 모은다",
                        "Supervisor 판단 기록은 30일 보관한다", "멀티 에이전트 시험은 PoC로만 한다"],
    "routing": ["라우팅 결과는 로그에 남긴다", "질문 분류 예시는 20개로 한다", "라우팅 규칙표는 위키에 둔다",
                "경로 분기 오류는 일반 대화로 넘긴다", "질문 분류 기준은 분기마다 본다", "라우팅 시험 문항은 50개로 한다"],
    "llm.tiering": ["모델 티어링 비교표는 위키에 둔다", "작은 모델 후보는 3개로 줄인다", "큰 모델 응답은 캐시하지 않는다",
                    "티어링 결과 보고는 월 1회 한다", "모델 분리 설정은 환경 변수로 둔다", "작은 모델 시험은 개발망에서 한다"],
}


def fakes(n: int, seed: int = 7) -> list[dict]:
    """
    가짜 결정 n건 (15%는 같은 주제의 다른 사안, 나머지는 다른 사안)
    Args:
        n:    만들 건수
        seed: 섞기 씨앗
    Returns:
        결정 레코드 목록 (승인·유효, 근거 없음)
    """
    rng = random.Random(seed)
    same = [(t, s) for t, ss in SAME_TOPIC.items() for s in ss]
    other = [(f"misc.{i:02d}", f"{subj}은 {way}로 한다") for i, subj in enumerate(OTHER_SUBJECTS) for way in WAYS]
    rng.shuffle(same)
    rng.shuffle(other)
    k = min(len(same), round(n * 0.15))
    rows = same[:k] + other[:n - k]
    out = []
    for i, (topic, st) in enumerate(rows):
        pid, key = ("PRJ-CSBOT", "CSB") if i % 5 == 4 else ("PRJ-OPSAI", "OPS")
        out.append({"decision_id": f"{key}-{1000 + i}", "project_id": pid, "topic": topic, "statement": st + ".",
                    "rationale": "", "alternatives_rejected": [], "decided_by": "가짜", "status": "approved",
                    "effective_from": "2026-08-01", "evidence": []})
    return out


class LocalMCP:
    """retrieve.pick이 부르는 MCP 도구 두 개를 같은 프로세스의 색인 함수로 대신한다"""

    def __init__(self, store: LedgerStore, records: MeetingRecords):
        """
        Args:
            store:   원장 (시드 + 가짜 결정)
            records: 회의 기록
        """
        self.store, self.records = store, records

    async def call(self, name: str, **a):
        """
        도구 호출 흉내
        Args:
            name: 도구 이름 (index_candidates · project_records_all)
            a:    도구 인자
        Returns:
            mcp_ledger_server의 같은 이름 도구와 같은 결과
        """
        if name == "index_candidates":
            period = (a["date_from"], a["date_to"]) if a.get("date_from") else None
            return find_candidates(self.store, self.records, a["project_id"], a["topics"], a["decision_ids"],
                                   a["meeting_ids"], a["words"], period)
        if name == "project_records_all":
            return project_records(self.store, self.records, a["project_id"])
        raise KeyError(name)


def build(size: int, tmp: Path) -> tuple[LedgerStore, MeetingRecords]:
    """
    목록 크기 size가 되도록 시드 원장에 가짜 결정을 섞은 임시 원장
    Args:
        size: 읽을 수 있는 결정(유효·승인 대기) 총수
        tmp:  임시 폴더
    Returns:
        (원장, 회의 기록)
    """
    db = tmp / f"ledger_{size}.db"
    store = LedgerStore(db)
    store.load_seed()
    records = MeetingRecords(db)
    records.sync_meetings()
    real = sum(1 for d in store.decisions() if d["is_active"] or d["status"] == "proposed")
    for d in fakes(max(0, size - real)):
        store._insert_decision(d)  # pylint: disable=protected-access  # 시험용 가짜 결정은 propose 절차 없이 넣는다
    return store, records


def score(picked: list[str], expected: list[str]) -> tuple[bool, int]:
    """
    한 질문 채점
    Args:
        picked:   고른 결정 ID
        expected: 기대 결정 ID (빈 목록 = 결정 없음)
    Returns:
        (정답 여부, 오선택 수)
    """
    wrong = len([p for p in picked if p not in expected])
    ok = set(expected) <= set(picked) if expected else not picked
    return ok, wrong


async def one(llm, store, records, q, pid, exp, mode: str, everything: list[dict]) -> dict:  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """
    질문 하나를 한 방식으로
    Args:
        llm:        선택 모델
        store:      원장
        records:    회의 기록
        q:          질문
        pid:        질문 프로젝트
        exp:        기대 결정 ID
        mode:       full | chunked | narrowed
        everything: 전체 목록 (모든 프로젝트, 한 방식 안에서 같은 순서)
    Returns:
        {"ok", "wrong", "sec", "tokens", "shown", "calls", "how", "pos"}
    """
    t0 = time.perf_counter()
    if mode == "narrowed":
        deps = type("D", (), {"mcp": LocalMCP(store, records), "quality_llm": llm})()
        out = await R.pick(deps, q, pid, project_key=pid.split("-")[1][:3])
        chosen, usages, shown, how = out["decisions"], out["usages"], out["shown"], out["how"]
        listed = {k[2:] for k in out["via"]} if how == "색인" else {c.get("decision_id") for c in everything}
    else:
        budget = 10 ** 9 if mode == "full" else R.LIST_TOKEN_BUDGET
        chosen, usages = await R.read_list(llm, q, everything, budget=budget)
        shown, how = len(everything), mode
        listed = {c.get("decision_id") for c in everything}
    picked = [c["decision_id"] for c in chosen if c["kind"] == "decision"]
    ok, wrong = score(picked, exp)
    keys = [c.get("decision_id") for c in everything]
    pos = [round(keys.index(e) / max(1, len(keys) - 1), 2) for e in exp if e in keys]
    return {"q": q, "ok": ok, "wrong": wrong, "in_list": set(exp) <= listed, "picked": picked, "sec": round(time.perf_counter() - t0, 2),
            "tokens": sum(u.get("tokens", 0) for u in usages if u), "calls": len(usages), "shown": shown,
            "how": how, "pos": pos}


def others_running() -> list[str]:
    """
    시간 측정을 흐리는 다른 평가 프로세스
    Returns:
        pgrep 결과 줄 (자기 자신 제외)
    """
    r = subprocess.run(["pgrep", "-fl", "run_eval|reliability/run.py|process_meetings|list_scaling"],
                       capture_output=True, text=True, check=False)
    return [ln for ln in r.stdout.splitlines() if ln.split()[0] != str(__import__("os").getpid())]


def to_md(res: dict, sizes: list[int]) -> str:
    """
    결과 표와 한 줄 결론
    Args:
        res:   {크기: {방식: [질문별 결과]}}
        sizes: 목록 크기
    Returns:
        Markdown
    """
    def agg(rows):
        n = len(rows)
        return {"acc": round(100 * sum(r["ok"] for r in rows) / n), "wrong": sum(r["wrong"] for r in rows),
                "sec": round(sum(r["sec"] for r in rows) / n, 1), "tok": round(sum(r["tokens"] for r in rows) / n),
                "shown": round(sum(r["shown"] for r in rows) / n, 1),
                "full": sum(r["how"] == "전체" for r in rows),
                "hit": round(100 * sum(r["in_list"] for r in rows) / n)}
    head = ("| 목록 크기 | 전체 읽기 정답(%) | 오선택(건) | 평균 초 | 평균 토큰 | 나눠 읽기 정답(%) | 평균 초 | 평균 토큰 "
            "| 좁힌 뒤 정답(%) | 후보 안 정답 포함(%) | 오선택(건) | 읽은 후보(평균) | 평균 초 | 평균 토큰 | 전체 읽기로 간 질문 |")
    rows = [head, "|" + "---|" * 15]
    accs = {}
    for s in sizes:
        f, c, n = (agg(res[str(s)][m]) for m in ("full", "chunked", "narrowed"))
        accs[s] = (f["acc"], n["acc"])
        rows.append(f"| {s} | {f['acc']} | {f['wrong']} | {f['sec']} | {f['tok']} | {c['acc']} | {c['sec']} | {c['tok']} "
                    f"| {n['acc']} | {n['hit']} | {n['wrong']} | {n['shown']} | {n['sec']} | {n['tok']} | {n['full']} |")
    # 위치별 정답 (전체 읽기, 가장 큰 목록) — 목록 가운데에 있는 정답을 놓치는가
    big = res[str(sizes[-1])]["full"]
    buckets = {"앞 1/3": [], "가운데 1/3": [], "뒤 1/3": []}
    for r in big:
        for p in r["pos"]:
            buckets["앞 1/3" if p < 1 / 3 else "가운데 1/3" if p < 2 / 3 else "뒤 1/3"].append(r["ok"])
    pos = " · ".join(f"{k} {round(100 * sum(v) / len(v)) if v else '-'}% ({len(v)}문항)" for k, v in buckets.items())
    base = accs[sizes[0]][0]
    broke = next((s for s in sizes if accs[s][0] <= base - 10), None)
    concl = (f"전체 읽기는 {broke}건부터 정답률이 {base}% → {accs[broke][0]}%로 떨어진다"
             if broke else f"전체 읽기 정답률은 {sizes[-1]}건까지 {base}%에서 10%p 넘게 떨어지지 않았다")
    concl += (f". 좁힌 뒤 읽기는 {sizes[-1]}건에서도 {accs[sizes[-1]][1]}%"
              f" (읽는 후보 평균 {agg(res[str(sizes[-1])]['narrowed'])['shown']}건).")
    n_q = len(big)
    return ("# 목록 크기 곡선\n\n"
            f"질문 {n_q}개(근거 질의 6 + 간단한 질문 20)를 목록 크기별로 세 방식으로 읽혔다. 모델 로컬 qwen3:8b "
            "(think off, temperature 0, num_ctx 16384). 진짜 결정 11건에 가짜 결정을 섞어 크기를 맞췄다 "
            "(가짜의 15%는 같은 주제의 다른 사안). 정답 = 기대 결정 ID를 고름(기대가 없으면 아무 결정도 안 고름).\n\n"
            + "\n".join(rows)
            + f"\n\n전체 읽기 {sizes[-1]}건에서 정답 위치별 정답률: {pos}\n\n**결론** {concl}\n")


async def main(sizes: list[int], force: bool) -> None:
    """
    크기별로 세 방식을 돌리고 저장
    Args:
        sizes: 목록 크기
        force: 다른 평가가 돌고 있어도 진행
    """
    busy = others_running()
    if busy and not force:
        print("다른 평가가 돌고 있어 시간 측정을 미룬다:\n" + "\n".join(busy))
        return
    llm = LocalLLM("qwen3:8b")
    await llm.json("ID만 고른다", "준비", R.PICK_SCHEMA, max_tokens=5)       # 모델 적재 시간을 첫 측정에서 뺀다
    qs, res = questions(), {}
    tmp = Path(tempfile.mkdtemp(prefix="list_scaling_"))
    for size in sizes:
        store, records = build(size, tmp)
        everything = []
        for pid in ("PRJ-OPSAI", "PRJ-CSBOT"):
            everything += project_records(store, records, pid)
        random.Random(size).shuffle(everything)            # 진짜 결정이 목록 앞에 몰리지 않게
        res[str(size)] = {}
        for mode in ("full", "chunked", "narrowed"):
            rows = []
            for q, pid, exp in qs:
                rows.append(await one(llm, store, records, q, pid, exp, mode, everything))
            res[str(size)][mode] = rows
            ok = sum(r["ok"] for r in rows)
            print(f"{size}건 {mode}: 정답 {ok}/{len(rows)} · 평균 {sum(r['sec'] for r in rows) / len(rows):.1f}초", flush=True)
    (ROOT / "eval" / "list_scaling.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    md = to_md(res, sizes)
    (ROOT / "eval" / "list_scaling.md").write_text(md)
    print(md)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="50,100,200,400")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    asyncio.run(main([int(x) for x in a.sizes.split(",")], a.force))
