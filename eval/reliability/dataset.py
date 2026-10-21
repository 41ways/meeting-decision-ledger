"""신뢰도 검증 데이터셋 — 세계(world) 합치기, 정답 읽기, 원장 시뮬레이션, 검사.

데이터셋은 시연 데이터(data/project)와 분리해 eval/reliability 아래에 둔다.
  world/     data/project에 덧붙는 프로젝트·인물·주제·정책·예외·기준 문서·원장 시드
  meetings/  회의 전사문 40건 (r1_meetings와 같은 형식)
  gold/      프로젝트별 정답 (회의 결정·할 일·미결·함정, 의견, 질문)

정답의 결정 ID는 원장 번호가 실행 순서로 정해지므로 '{EXP-M03-D2}'처럼 정답 ID로 적고,
원장 시뮬레이션(Ledger)이 실제로 발급될 번호로 바꾼다. 러너도 같은 순서로 원장에 반영한다.

사용: .venv/bin/python eval/reliability/dataset.py --check   (LLM 없이 규모·정답 일관성 검사)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REL = ROOT / "eval" / "reliability"
BASE = ROOT / "data" / "project"
WORLD = REL / "world"
MEETINGS = REL / "meetings"
GOLD = REL / "gold"

KEYS = {"OPS": "PRJ-OPSAI", "EXP": "PRJ-EXPAUTO", "FLD": "PRJ-FIELDAPP", "SLS": "PRJ-SALESDASH", "CSB": "PRJ-CSBOT"}
PID_KEY = {v: k for k, v in KEYS.items()}
# 발표용 규모 — 분할(개발·시험)마다 정확히 이 수여야 한다
TARGET = {"meetings": 20, "decisions": 100, "action_items": 50, "opinions": 30, "questions": 100, "not_found": 20}
BLOCKING = {"policy", "authority", "past_decision", "baseline"}
CONFLICT_TYPES = BLOCKING | {"past_project", "change_approved", "confirms"}
STATES = {"확정", "승인 대기", "미결", "찾지 못함"}
VERDICTS = {"conflict", "allowed_with_waiver", "no_conflict", "needs_approval"}
OUTCOMES = {"approved", "rejected", "pending", "confirms"}
# 작성자마다 조금씩 다르게 쓴 현상 이름을 하나로
TAG_ALIAS = {"회의 밖 결정 언급": "회의 밖 결정", "확정 표현 없는 진행자 단정": "진행자 단정", "말끊김": "말 끊김"}
REF = re.compile(r"\{([A-Z]{3}-M\d{2}-[DAU]\d+)\}")
SEED_ID = re.compile(r"^[A-Z]{3}-\d{4}$")


def _j(path: Path) -> dict:
    """
    JSON 파일 읽기
    Args:
        path: 파일 경로
    Returns:
        파싱한 값
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _w(path: Path, data) -> None:
    """
    JSON 파일 쓰기 (한글 그대로, 들여쓰기 1)
    Args:
        path: 파일 경로
        data: 저장할 값
    """
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 세계 합치기
def merged_seed() -> list[dict]:
    """
    시연 시드 결정과 데이터셋 시드를 합친 원장 시드
    Returns:
        결정 목록. 같은 decision_id는 데이터셋 값이 이긴다
    """
    seed = {d["decision_id"]: d for d in _j(BASE / "r3_ledger" / "decisions_seed.json")["decisions"]}
    for d in _j(WORLD / "r3_ledger" / "decisions_seed.json")["decisions"]:
        seed[d["decision_id"]] = d
    return list(seed.values())


def build_data_dir(dst: Path, meetings: list[str]) -> None:
    """
    평가용 data/project 폴더를 만든다 — 시연 데이터 복사본 + 데이터셋 세계 + 지금까지 열린 회의 전사문

    시연 폴더는 읽기만 한다. 회의 전사문은 meetings에 든 것만 넣어 미래 회의가 검색에 섞이지 않게 한다.
    Args:
        dst:      만들 폴더 (data/project 자리)
        meetings: 넣을 데이터셋 회의 ID 목록
    """
    if not dst.exists():
        shutil.copytree(BASE, dst)
    prj = _j(dst / "projects.json")
    if not any(p["key"] == "EXP" for p in prj["projects"]):
        prj["projects"] += _j(WORLD / "projects.json")["projects"]
        _w(dst / "projects.json", prj)
        users = _j(dst / "users.json")
        users["users"] += _j(WORLD / "users.json")["users"]
        _w(dst / "users.json", users)
        topics = _j(dst / "topics.json")
        topics["topics"].update(_j(WORLD / "topics.json")["topics"])
        _w(dst / "topics.json", topics)
        man = _j(dst / "r2_docs" / "manifest.json")
        docs = {(d["doc_id"], d["doc_version"]): d for d in man["documents"]}
        for d in _j(WORLD / "r2_docs" / "manifest.json")["documents"]:
            docs[(d["doc_id"], d["doc_version"])] = d
        man["documents"] = list(docs.values())
        _w(dst / "r2_docs" / "manifest.json", man)
        for f in (WORLD / "r2_docs").glob("*.md"):
            shutil.copy(f, dst / "r2_docs" / f.name)
        _w(dst / "r3_ledger" / "decisions_seed.json", {"decisions": merged_seed()})
        pol = _j(dst / "r4_governance" / "policies.json")
        add = _j(WORLD / "r4_governance" / "policies.json")["add"]
        for v in pol["versions"]:
            v["rules"] += add.get(v["policy_version"], [])
        _w(dst / "r4_governance" / "policies.json", pol)
        wv = _j(dst / "r4_governance" / "waivers.json")
        wv["waivers"] += _j(WORLD / "r4_governance" / "waivers.json")["waivers"]
        _w(dst / "r4_governance" / "waivers.json", wv)
    for mid in meetings:
        shutil.copy(MEETINGS / f"{mid}.json", dst / "r1_meetings" / f"{mid}.json")


def real_names() -> set[str]:
    """
    시연 데이터의 기존 인물 이름 — 이 사람들은 데이터셋에서 문제 행동을 하지 않는다
    Returns:
        이름 집합
    """
    return {u["name"] for u in _j(BASE / "users.json")["users"]}


def world_people() -> dict[str, dict]:
    """
    데이터셋에 새로 넣은 가상 인물
    Returns:
        이름 → 사용자 정보
    """
    return {u["name"]: u for u in _j(WORLD / "users.json")["users"]}


def all_topics() -> set[str]:
    """
    시연 주제와 데이터셋 주제를 합친 주제 키 집합
    Returns:
        주제 키 집합
    """
    return set(_j(BASE / "topics.json")["topics"]) | set(_j(WORLD / "topics.json")["topics"])


def baseline_refs() -> set[str]:
    """
    현행 기준 문서 조항 번호 (판정에서 쓰는 ref = '## ' 제목의 첫 단어)
    Returns:
        조항 번호 집합 (REQ-03, EREQ-02 …)
    """
    refs = set()
    for f in list((BASE / "r2_docs").glob("*.md")) + list((WORLD / "r2_docs").glob("*.md")):
        refs |= set(re.findall(r"^## (\S+)", f.read_text(encoding="utf-8"), re.M))
    return refs


def rule_ids() -> set[str]:
    """
    현행 정책 조항 ID
    Returns:
        규칙 ID 집합
    """
    ids = {r["rule_id"] for v in _j(BASE / "r4_governance" / "policies.json")["versions"] if v["is_current"]
           for r in v["rules"]}
    return ids | {r["rule_id"] for r in _j(WORLD / "r4_governance" / "policies.json")["add"].get("v3", [])}


# ---------------------------------------------------------------- 정답 읽기
def load_gold() -> dict:
    """
    gold/*.json을 하나로 합친다
    Returns:
        {"meetings": {회의 ID: 정답}, "opinions": [...], "questions": [...]}. 회의 정답에 project·date를 붙인다
    """
    out = {"meetings": {}, "opinions": [], "questions": []}
    for f in sorted(GOLD.glob("*.json")):
        g = _j(f)
        for mid, m in g.get("meetings", {}).items():
            doc = _j(MEETINGS / f"{mid}.json") if (MEETINGS / f"{mid}.json").exists() else {}
            for x in [m, *m["decisions"], *m["action_items"], *m.get("undecided", [])]:
                for k in ("phenomena", "tags"):
                    if k in x:
                        x[k] = list(dict.fromkeys(TAG_ALIAS.get(t, t) for t in x[k]))
            out["meetings"][mid] = {**m, "project": mid.split("-")[0], "date": doc.get("date", "")}
        out["opinions"] += g.get("opinions", [])
        out["questions"] += g.get("questions", [])
    return out


def ordered(gold: dict, split: str | None = None) -> list[str]:
    """
    회의를 열린 순서(날짜, ID)로
    Args:
        gold:  load_gold 결과
        split: dev / test 로 한정. None이면 전부
    Returns:
        회의 ID 목록
    """
    ms = [(m["date"], mid) for mid, m in gold["meetings"].items() if split in (None, m["split"])]
    return [mid for _, mid in sorted(ms)]


# ---------------------------------------------------------------- 원장 시뮬레이션
class Ledger:
    """
    원장 상태를 LLM 없이 따라가는 시뮬레이터 (store.propose·approve·reject와 같은 규칙)

    팀장이 정답 결과(outcome)대로 검토했다고 가정한다. 러너는 같은 순서로 MCP 도구를 불러 실제 원장을 만든다.
    """

    def __init__(self):
        """시드로 원장을 채운다"""
        self.d = {x["decision_id"]: {"project_id": x["project_id"], "topic": x["topic"], "status": x["status"],
                                     "superseded_by": x.get("superseded_by"), "statement": x["statement"]}
                  for x in merged_seed()}
        self.gid: dict[str, str] = {}          # 정답 결정 ID → 원장 ID

    def next_id(self, key: str) -> str:
        """
        다음 결정 번호 (프로젝트 약자별 최댓값 + 1)
        Args:
            key: 프로젝트 약자
        Returns:
            결정 ID
        """
        n = max((int(i.split("-")[1]) for i in self.d if i.startswith(key + "-")), default=0) + 1
        return f"{key}-{n:04d}"

    def approve(self, did: str) -> list[str]:
        """
        승인 — 같은 프로젝트·주제의 다른 유효 결정은 대체된다
        Args:
            did: 원장 ID
        Returns:
            대체된 결정 ID 목록
        """
        me = self.d[did]
        prev = [i for i, x in self.d.items() if i != did and x["project_id"] == me["project_id"]
                and x["topic"] == me["topic"] and x["status"] == "approved" and not x["superseded_by"]]
        for i in prev:
            self.d[i]["superseded_by"] = did
        me["status"] = "approved"
        return prev

    def apply(self, mid: str, m: dict) -> None:
        """
        회의 하나의 정답 결정을 원장에 반영 (결정 발언 순서대로)
        Args:
            mid: 회의 ID
            m:   그 회의 정답
        """
        for gd in sorted(m["decisions"], key=lambda x: x["seq"]):
            if gd["outcome"] == "confirms":
                target = self.ref(gd["confirms"])
                self.gid[gd["id"]] = target
                self.approve(target)
                continue
            did = self.next_id(m["project"])
            self.d[did] = {"project_id": KEYS[m["project"]], "topic": gd["topic"], "status": "proposed",
                           "superseded_by": None, "statement": gd["statement"]}
            self.gid[gd["id"]] = did
            if gd["outcome"] == "approved":
                self.approve(did)
            elif gd["outcome"] == "rejected":
                self.d[did]["status"] = "rejected"

    def ref(self, x: str) -> str:
        """
        정답 참조 하나를 원장 ID로 ('{EXP-M02-D1}' 또는 'EXP-M02-D1' → EXP-0009, 시드 ID·규칙 ID는 그대로)
        Args:
            x: 참조
        Returns:
            원장 ID 또는 원래 값
        """
        x = x.strip("{}")
        return self.gid.get(x, x)

    def text(self, s: str) -> str:
        """
        문장 속 '{정답 ID}'를 원장 ID로 바꾼다
        Args:
            s: 문장
        Returns:
            바꾼 문장
        """
        return REF.sub(lambda mo: self.gid.get(mo.group(1), mo.group(0)), s)

    def active(self, did: str) -> bool:
        """
        유효 결정인지 (승인 + 대체 안 됨)
        Args:
            did: 원장 ID
        Returns:
            유효하면 True
        """
        x = self.d.get(did)
        return bool(x and x["status"] == "approved" and not x["superseded_by"])


def id_map(gold: dict) -> dict[str, str]:
    """
    전체 시간순(개발 → 시험)으로 시뮬레이션해 정답 결정 ID → 원장 ID 표를 만든다
    Args:
        gold: load_gold 결과
    Returns:
        정답 ID → 원장 ID
    """
    led = Ledger()
    for mid in ordered(gold):
        led.apply(mid, gold["meetings"][mid])
    return led.gid


# ---------------------------------------------------------------- 검사
def check(gold: dict) -> tuple[list[str], list[str], dict]:
    """
    규모·형식·정답 일관성 검사

    오류: 규모 불일치, 없는 발언 번호, 모르는 주제·규칙·조항, 문제 행동을 기존 인물이 함, 기대 상태가 원장과 모순
    경고: 판단이 갈릴 수 있는 부분 (사람 검수 권장)
    Args:
        gold: load_gold 결과
    Returns:
        (오류 목록, 경고 목록, 분할별 통계)
    """
    err, warn = [], []
    real, people, topics = real_names(), world_people(), all_topics()
    rules, brefs = rule_ids(), baseline_refs()
    stats = {s: Counter() for s in ("dev", "test")}
    tags = {s: Counter() for s in ("dev", "test")}
    gids = set()
    for mid, m in gold["meetings"].items():
        f = MEETINGS / f"{mid}.json"
        if not f.exists():
            err.append(f"{mid}: 전사문 없음")
            continue
        doc = _j(f)
        segs = {s["seq"]: s for s in doc["segments"]}
        if list(segs) != list(range(1, len(segs) + 1)):
            err.append(f"{mid}: 발언 번호가 1부터 연속이 아님")
        if doc["project_id"] != KEYS[m["project"]] or doc["meeting_id"] != mid:
            err.append(f"{mid}: meeting_id·project_id 불일치")
        for s in doc["segments"]:
            if s["speaker"] not in doc["attendees"]:
                err.append(f"{mid}#{s['seq']}: 참석자 아닌 화자 {s['speaker']}")
            if s["speaker"] not in real and s["speaker"] not in people:
                err.append(f"{mid}#{s['seq']}: 인물 목록에 없는 화자 {s['speaker']}")
            p = people.get(s["speaker"])
            if p and p["speaker_role"] != s["role"]:
                warn.append(f"{mid}#{s['seq']}: {s['speaker']} 역할 {s['role']} (목록 {p['speaker_role']})")
        sp = m["split"]
        stats[sp]["meetings"] += 1
        tags[sp].update(m.get("phenomena", []))
        for gd in m["decisions"]:
            stats[sp]["decisions"] += 1
            gids.add(gd["id"])
            tags[sp].update(gd.get("tags", []))
            if gd["seq"] not in gd["evidence_seq"] or any(q not in segs for q in gd["evidence_seq"]):
                err.append(f"{gd['id']}: 근거 발언 번호 오류")
            elif segs[gd["seq"]]["speaker"] != gd["decided_by"]:
                warn.append(f"{gd['id']}: 결정자 {gd['decided_by']} ≠ 확정 발언 화자 {segs[gd['seq']]['speaker']}")
            if gd["topic"] not in topics:
                err.append(f"{gd['id']}: 모르는 주제 {gd['topic']}")
            if gd["outcome"] not in OUTCOMES:
                err.append(f"{gd['id']}: outcome {gd['outcome']}")
            kinds = {c["type"] for c in gd.get("conflicts", [])}
            if kinds - CONFLICT_TYPES:
                err.append(f"{gd['id']}: 모르는 충돌 종류 {kinds - CONFLICT_TYPES}")
            want = "확인 필요" if kinds & BLOCKING else "승인 대기"
            if gd["expected_label"] != want and not gd.get("ambiguous"):
                err.append(f"{gd['id']}: expected_label {gd['expected_label']} ≠ 충돌 기준 {want}")
            if kinds & BLOCKING and gd["outcome"] in ("approved", "confirms"):
                warn.append(f"{gd['id']}: 충돌이 있는데 승인으로 처리")
            # 문제 행동(권한·규정 위반)은 가상 인물만 — 제안·확정 발언 화자 모두
            if kinds & {"authority", "policy"}:
                bad = {segs[q]["speaker"] for q in gd["evidence_seq"] if q in segs} & real
                if bad:
                    err.append(f"{gd['id']}: 기존 인물 {bad}이 권한·규정 위반 결정에 관여")
            for c in gd.get("conflicts", []):
                for a in c["against"]:
                    if c["type"] in ("policy", "authority") and a not in rules:
                        err.append(f"{gd['id']}: 모르는 규칙 {a}")
                    if c["type"] == "baseline" and a not in brefs:
                        err.append(f"{gd['id']}: 모르는 조항 {a}")
            if gd["outcome"] == "confirms" and not gd.get("confirms"):
                err.append(f"{gd['id']}: confirms 대상 없음")
        for a in m["action_items"]:
            stats[sp]["action_items"] += 1
            tags[sp].update(a.get("tags", []))
            if a["seq"] not in a["evidence_seq"] or any(q not in segs for q in a["evidence_seq"]):
                err.append(f"{a['id']}: 근거 발언 번호 오류")
            if a.get("owner") and a["owner"] not in doc["attendees"]:
                warn.append(f"{a['id']}: 담당자 {a['owner']}가 참석자 아님")
            if a.get("due") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a["due"]):
                err.append(f"{a['id']}: 기한 형식 {a['due']}")
        for u in m.get("undecided", []):
            stats[sp]["undecided"] += 1
            tags[sp].update(u.get("tags", []))
            if any(q not in segs for q in u["evidence_seq"]) or u["topic"] not in topics:
                err.append(f"{u['id']}: 근거 번호·주제 오류")
        for t in m.get("traps", []):
            stats[sp]["traps"] += 1
            tags[sp][f"함정:{t['kind']}"] += 1
            if t["seq"] not in segs:
                err.append(f"{mid} 함정 {t['seq']}: 없는 발언")
    # 시간순 시뮬레이션 — 의견·질문의 시점에 원장 상태가 정답과 맞는지
    led = Ledger()
    at = defaultdict(list)
    for x in gold["opinions"] + gold["questions"]:
        at[x["at"]].append(x)
        if x["at"] not in gold["meetings"]:
            err.append(f"{x['id']}: 시점 {x['at']}이 데이터셋 회의가 아님")
        elif gold["meetings"][x["at"]]["split"] != x["split"]:
            err.append(f"{x['id']}: 시점 회의와 분할이 다름")
    opens: dict[str, dict] = {}
    for mid in ordered(gold):
        m = gold["meetings"][mid]
        # 기존 결정과의 관계는 회의 시점 원장 기준: 다름·변경 = 같은 프로젝트·주제의 유효 결정, 확정 = 승인 대기 결정
        for gd in m["decisions"]:
            for c in gd.get("conflicts", []):
                if c["type"] not in ("past_decision", "change_approved", "confirms"):
                    continue
                for a in c["against"]:
                    rid = led.ref(a)
                    x = led.d.get(rid)
                    if not x:
                        if SEED_ID.match(rid) or REF.fullmatch(a):
                            err.append(f"{gd['id']}: {c['type']} 대상 {a} 없음")
                        continue
                    if c["type"] == "confirms" and x["status"] != "proposed":
                        err.append(f"{gd['id']}: confirms 대상 {a}({rid})가 승인 대기가 아님")
                    elif c["type"] != "confirms" and not led.active(rid):
                        err.append(f"{gd['id']}: {c['type']} 대상 {a}({rid})가 유효 결정이 아님")
                    elif x["project_id"] != KEYS[m["project"]] or x["topic"] != gd["topic"]:
                        warn.append(f"{gd['id']}: {c['type']} 대상 {a}({rid})는 주제 {x['topic']} — 결정 주제 {gd['topic']}와 다름")
        led.apply(mid, m)
        for u in m.get("undecided", []):
            opens[u["id"]] = {**u, "project": m["project"]}
        for x in at[mid]:
            if "expected" in x:
                stats[x["split"]]["opinions"] += 1
                if x["expected"] not in VERDICTS:
                    err.append(f"{x['id']}: verdict {x['expected']}")
                continue
            stats[x["split"]]["questions"] += 1
            stats[x["split"]][f"질문:{x['expected_state']}"] += 1
            stats[x["split"]][f"질문 종류:{x.get('kind', '?')}"] += 1
            if x["expected_state"] == "찾지 못함":
                stats[x["split"]]["not_found"] += 1
            if x["expected_state"] not in STATES:
                err.append(f"{x['id']}: 상태 {x['expected_state']}")
            for r in x.get("expected_ids", []):
                rid = led.ref(r)
                if REF.fullmatch(r) and r.strip("{}") not in led.gid and r.strip("{}") not in opens:
                    err.append(f"{x['id']}: 아직 없는 정답 ID {r}")
                elif x["expected_state"] == "확정" and SEED_ID.match(rid) and not led.active(rid):
                    err.append(f"{x['id']}: {r}({rid})가 시점 {mid}에 유효 결정이 아님")
                elif x["expected_state"] == "승인 대기" and SEED_ID.match(rid) and led.d.get(rid, {}).get("status") != "proposed":
                    err.append(f"{x['id']}: {r}({rid})가 시점 {mid}에 승인 대기가 아님")
            if x.get("follow_up_of") and x["follow_up_of"] not in {q["id"] for q in gold["questions"]}:
                err.append(f"{x['id']}: 이어 묻기 대상 없음")
    for s in ("dev", "test"):
        for k, n in TARGET.items():
            if stats[s][k] != n:
                err.append(f"{s}: {k} {stats[s][k]} (목표 {n})")
    return err, warn, {"stats": stats, "tags": tags}


def main() -> None:
    """
    --check: 규모·일관성 검사 결과와 분할별 통계를 출력. 오류가 있으면 종료 코드 1
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--project", help="이 프로젝트 정답만 보고 규모 목표는 건너뜀 (작성 중 확인용)")
    args = ap.parse_args()
    gold = load_gold()
    if args.project:
        gold["meetings"] = {k: v for k, v in gold["meetings"].items() if v["project"] == args.project}
        gold["opinions"] = [x for x in gold["opinions"] if x["id"].startswith(args.project)]
        gold["questions"] = [x for x in gold["questions"] if x["id"].startswith(args.project)]
    err, warn, st = check(gold)
    if args.project:
        err = [e for e in err if "(목표" not in e]
    for s in ("dev", "test"):
        print(f"[{s}]", dict(sorted(st["stats"][s].items())))
        print(f"  현상·함정:", dict(sorted(st["tags"][s].items())))
    print(f"\n경고 {len(warn)}건")
    for w in warn:
        print("  -", w)
    print(f"오류 {len(err)}건")
    for e in err:
        print("  -", e)
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
