"""파이프라인 승격·롤백 — challenger를 기준 평가로 돌려 champion과 비교한다 (docs/pipeline_ops.md §4).

사용:
  .venv/bin/python scripts/promote.py                     지금 버전을 평가해 champion과 비교 → 승격 또는 거부
  .venv/bin/python scripts/promote.py --dry-run           평가·비교만 (레지스트리 상태는 그대로)
  .venv/bin/python scripts/promote.py --runner reliability 신뢰도 데이터셋 개발 셋으로 평가 (eval/reliability/run.py)
  .venv/bin/python scripts/promote.py --from-results F    평가를 돌리지 않고 결과 파일 F로 비교
  .venv/bin/python scripts/promote.py --rollback [--apply] 바로 앞 champion으로 되돌리고 복원 방법 안내(--apply면 임계값 복원)
  .venv/bin/python scripts/promote.py --status            버전 목록
  .venv/bin/python scripts/promote.py --ack-policy        정책 개정 경보 확인 처리 (지금 정책 버전을 본 것으로)

판정: 판정 지표('(%)'는 높을수록, '(건)'은 낮을수록 좋음)가 하나라도 champion보다 나쁘면 거부.
시간 지표('시간')가 champion 대비 +30% 넘게 늘면 경고(거부는 아님). 호출 수·토큰·비용은 참고로만 보인다.
champion이 아직 없으면 저장소에 커밋된 기준 결과(기본: HEAD의 eval/results.json)와 비교한다.
평가 스크립트가 덮어쓴 결과 파일(eval/results.*)은 끝나면 원래대로 돌려놓는다 — 공개 수치는 사람이 따로 갱신한다.
코드 되돌리기는 git 명령을 안내만 한다 (자동으로 git을 건드리지 않는다).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger import pipeline_registry as PR  # noqa: E402
from ledger.store import STORE_DIR  # noqa: E402

PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
# 기준 평가 러너 — 신뢰도 데이터셋이 합쳐지면 reliability로 바꿔 끼운다 (결과 파일 경로는 그 러너에 맞춰 고친다)
RUNNERS = {
    "after": {"cmd": [PY, "eval/run_eval.py", "--only", "after"], "result": "eval/results.json",
              "key": "After 결정 원장", "restore": ["eval/results.json", "eval/results.md"]},
    "reliability": {"cmd": [PY, "eval/reliability/run.py", "--split", "dev"],
                    "result": "eval/reliability/results_dev.json", "key": None, "restore": []},
}
TIME_WARN = 1.30
BUSY = "run_eval|reliability/run.py|process_meetings|list_scaling"


def read_scores(path: Path | str, key: str | None = None) -> dict:
    """
    평가 결과 파일에서 지표 사전을 꺼낸다

    {"scores": {방식: {지표: 값}}} (run_eval 형식), {"scores": {지표: 값}}, {지표: 값} 모두 받는다.
    Args:
        path: 결과 JSON 경로
        key:  방식 이름 (run_eval의 'After 결정 원장'). None이면 방식이 하나일 때 그것
    Returns:
        지표 → 값 (숫자 또는 None)
    Raises:
        ValueError: 지표를 찾지 못함
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    scores = data.get("scores", data)
    if key and isinstance(scores.get(key), dict):
        scores = scores[key]
    elif all(isinstance(v, dict) for v in scores.values()) and len(scores) == 1:
        scores = next(iter(scores.values()))
    nums = {k: v for k, v in scores.items() if isinstance(v, (int, float)) or v is None}
    if not nums:
        raise ValueError(f"지표를 찾지 못했습니다: {path} (key={key})")
    return nums


def direction(metric: str) -> str:
    """
    지표의 좋은 방향
    Args:
        metric: 지표 이름
    Returns:
        'higher' | 'lower' | 'time' | 'info'
    Examples:
        direction('결정 재현율(%)') -> 'higher'; direction('충돌 오탐(건)') -> 'lower'
    """
    if "시간" in metric or metric.endswith("_sec"):
        return "time"
    if "(%)" in metric or metric.endswith(("_rate", "_acc", "_pct")):
        return "higher"
    if "(건)" in metric or metric.endswith("_count"):
        return "lower"
    return "info"


def judge_promotion(champion: dict, challenger: dict, tolerance: float = 0.0) -> dict:
    """
    승격 판정 — 판정 지표가 하나라도 떨어지면 거부, 시간이 30% 넘게 늘면 경고
    Args:
        champion:   champion(또는 기준 결과) 지표
        challenger: challenger 지표
        tolerance:  허용 하락 폭 (기본 0 — 조금이라도 떨어지면 거부)
    Returns:
        {"verdict": 'pass'|'reject', "drops": [...], "warnings": [...],
         "rows": [{metric, champion, challenger, dir, mark}]}
    """
    rows, drops, warns = [], [], []
    for m in dict.fromkeys([*champion, *challenger]):
        a, b, d = champion.get(m), challenger.get(m), direction(m)
        mark = ""
        if d in ("higher", "lower") and b is None and a is not None:
            mark = "값 없음"
            drops.append(f"{m}: challenger 값 없음")
        elif d in ("higher", "lower") and a is not None and b is not None:
            worse = (a - b) if d == "higher" else (b - a)
            if worse > tolerance:
                mark = "하락"
                drops.append(f"{m}: {a} → {b}")
        elif d == "time" and a and b and b > a * TIME_WARN:
            mark = "경고"
            warns.append(f"{m}: {a} → {b} (+{b / a - 1:.0%}, 기준 +{TIME_WARN - 1:.0%})")
        rows.append({"metric": m, "champion": a, "challenger": b, "dir": d, "mark": mark})
    return {"verdict": "reject" if drops else "pass", "drops": drops, "warnings": warns, "rows": rows}


def busy() -> list[str]:
    """
    평가 시간을 흐릴 다른 작업(평가·회의 일괄 정리)이 같은 맥에서 도는지
    Returns:
        돌고 있는 프로세스 줄 목록
    """
    out = subprocess.run(["pgrep", "-fl", BUSY], capture_output=True, text=True, check=False).stdout
    # 셸 명령 줄(zsh -c "… run_eval …")도 글자가 걸리므로 실제로 도는 파이썬 프로세스만 센다
    return [ln for ln in out.splitlines() if re.search(r"python", ln.split(" ")[1] if " " in ln else "", re.I)
            and "promote.py" not in ln]


def committed_scores(runner: dict) -> dict | None:
    """
    저장소에 커밋된 기준 결과 (champion이 없을 때 비교 기준)
    Args:
        runner: RUNNERS 항목
    Returns:
        지표 사전. 커밋된 파일이 없으면 None
    """
    try:
        text = subprocess.run(["git", "show", f"HEAD:{runner['result']}"], cwd=ROOT, capture_output=True,
                              text=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    tmp = STORE_DIR / "eval_runs" / "_committed.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    return read_scores(tmp, runner["key"])


def run_eval(runner: dict, vid: str) -> tuple[dict, Path]:
    """
    기준 평가 실행 — 덮어쓴 결과 파일은 원래대로 돌려놓고 이번 결과는 store/eval_runs/에 남긴다
    Args:
        runner: RUNNERS 항목
        vid:    평가하는 버전 ID (보관 파일 이름)
    Returns:
        (지표 사전, 보관한 결과 파일 경로)
    Raises:
        subprocess.CalledProcessError: 평가 스크립트 실패
    """
    backup = {p: (ROOT / p).read_bytes() for p in runner["restore"] if (ROOT / p).exists()}
    try:
        subprocess.run(runner["cmd"], cwd=ROOT, check=True)
        keep = STORE_DIR / "eval_runs" / f"{vid}-{datetime.now():%Y%m%d-%H%M%S}.json"
        keep.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads((ROOT / runner["result"]).read_text(encoding="utf-8"))
        # 결과만 보고도 어느 코드·모델·시각의 측정인지 알 수 있게 붙인다
        data["meta"] = {"version": vid, **PR.git_info(), "cmd": " ".join(runner["cmd"]),
                        "at": datetime.now().isoformat(timespec="seconds")}
        keep.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return read_scores(keep, runner["key"]), keep
    finally:
        for p, b in backup.items():
            (ROOT / p).write_bytes(b)


def print_table(j: dict, left: str) -> None:
    """
    비교표 출력
    Args:
        j:    judge_promotion 결과
        left: 왼쪽 열 이름 (champion 또는 기준 결과)
    """
    print(f"\n| 지표 | {left} | challenger | |\n|---|---|---|---|")
    for r in j["rows"]:
        print(f"| {r['metric']} | {r['champion']} | {r['challenger']} | {r['mark']} |")


def apply_thresholds(values: dict) -> list[str]:
    """
    임계값 파일의 상수를 주어진 값으로 고친다 (롤백 --apply)
    Args:
        values: 상수 이름 → 값
    Returns:
        고친 줄 설명 목록
    """
    path = ROOT / PR.THRESHOLD_FILE
    text, done = path.read_text(encoding="utf-8"), []
    for name, val in values.items():
        def keep_digits(m, val=val):
            # 0.50을 0.5로 바꿔 쓰면 값이 같아도 git 차이가 생긴다 — 원래 소수 자릿수를 지킨다
            old = m.group(2)
            places = len(old.split(".")[1]) if "." in old else 0
            return m.group(1) + (f"{val:.{places}f}" if places else str(val))
        new, n = re.subn(rf"^({re.escape(name)}\s*=\s*)([0-9.]+)", keep_digits, text, flags=re.M)
        if n:
            text = new
            done.append(f"{name} = {val}")
    path.write_text(text, encoding="utf-8")
    return done


def rollback(reg: PR.Registry, apply: bool, dry: bool) -> int:
    """
    바로 앞 champion으로 되돌리고, 지금 코드와 무엇이 다른지·어떻게 복원하는지 안내
    Args:
        reg:   레지스트리
        apply: True면 임계값을 파일에 복원
        dry:   True면 레지스트리를 바꾸지 않고 안내만
    Returns:
        종료 코드
    """
    lineage = reg.data["lineage"]
    if len(lineage) < 2:
        print("되돌아갈 이전 champion이 없습니다.")
        return 1
    target = reg.get(lineage[-2])
    if not dry:
        out = reg.rollback()
        reg.save()
        print(f"champion: {out['retired']} → {out['champion']} (내려간 버전은 retired)")
    else:
        print(f"[dry-run] champion을 {lineage[-1]} → {target['id']}로 되돌립니다")
    tm = target["components"]["models"]
    now = PR.components(tm["fast"], tm["quality"])
    diff = PR.diff_components(now, target["components"])
    commit = (target.get("git") or {}).get("commit") or "(커밋 기록 없음)"
    print(f"\n복원 대상 {target['id']} — 기준 커밋 {commit}")
    if diff["thresholds"]:
        vals = {k: v[1] for k, v in diff["thresholds"].items()}
        if apply and not dry:
            print("임계값 복원:", ", ".join(apply_thresholds(vals)))
        else:
            print("임계값이 다릅니다 (--apply로 복원):", ", ".join(f"{k} {a} → {b}" for k, (a, b) in diff["thresholds"].items()))
    if diff["models"]:
        print("모델이 다릅니다:", target["components"]["models"], "— LLM 설정(.env·PRESETS)을 맞춥니다.")
    files = sorted({k.split(":")[0] for k in diff["prompts"]} | set(diff["vocab"])
                   | (set(PR.CODE_FILES) if diff["code"] else set()))
    if files:
        print("프롬프트·주제 목록·코드가 다릅니다. 확인 후 직접 되돌립니다 (자동 git 조작 없음):")
        print(f"  git diff {commit} -- {' '.join(files)}")
        print(f"  git checkout {commit} -- {' '.join(files)}")
    if not (diff["thresholds"] or diff["models"] or files):
        print("지금 파일이 대상 버전과 같습니다. 서버만 다시 띄우면 됩니다.")
    print("복원 뒤 서버를 다시 띄우면 시작할 때 버전이 다시 계산됩니다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    명령행 진입점
    Args:
        argv: 인자 목록 (테스트용). None이면 sys.argv
    Returns:
        종료 코드 (0 승격·안내 완료, 1 거부·실패, 2 다른 평가가 돌고 있음)
    """
    ap = argparse.ArgumentParser(description="파이프라인 승격·롤백")
    ap.add_argument("--runner", choices=list(RUNNERS), default="after")
    ap.add_argument("--eval-cmd", help="평가 명령을 직접 지정 (공백 구분)")
    ap.add_argument("--eval-result", help="평가 결과 파일 경로")
    ap.add_argument("--eval-key", help="결과 파일 안의 방식 이름")
    ap.add_argument("--from-results", help="평가를 돌리지 않고 이 결과 파일로 비교")
    ap.add_argument("--tolerance", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-busy", action="store_true", help="다른 평가가 돌고 있어도 실행 (시간 지표가 흐려짐)")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ack-policy", action="store_true")
    ap.add_argument("--fast", default="ollama:qwen3:8b")
    ap.add_argument("--quality", default="ollama:qwen3:8b")
    ap.add_argument("--registry", default=str(PR.REGISTRY))
    a = ap.parse_args(argv)
    reg = PR.Registry(a.registry)

    if a.status:
        for v in reg.versions():
            ev = (v.get("evals") or [{}])[-1]
            print(f"{v['id']}  {v['status']:<10} 등록 {v['registered_at']}  승격 {v.get('promoted_at') or '-'}  "
                  f"커밋 {((v.get('git') or {}).get('commit') or '-')[:8]}  최근 평가 {ev.get('verdict', '-')}")
        print("champion:", reg.data.get("champion"), "· 승격 이력:", " → ".join(reg.data["lineage"]) or "-")
        return 0
    if a.ack_policy:
        from ledger.store import LedgerStore  # pylint: disable=import-outside-toplevel
        cur = LedgerStore().current_policy_version()
        reg.data["policy_seen"] = cur
        reg._log("policy_seen", reg.data.get("champion") or "-", policy=cur)  # pylint: disable=protected-access
        reg.save()
        print("정책 버전 확인:", cur)
        return 0
    if a.rollback:
        return rollback(reg, a.apply, a.dry_run)

    runner = dict(RUNNERS[a.runner])
    if a.eval_cmd:
        runner["cmd"] = a.eval_cmd.split()
    if a.eval_result:
        runner["result"] = a.eval_result
    if a.eval_key:
        runner["key"] = a.eval_key
    comp = PR.components(a.fast, a.quality)
    vid = PR.version_id(comp)
    entry = reg.register(comp, PR.git_info(), note="승격 평가 때 등록")
    print(f"challenger {vid} ({entry['status']}) · 커밋 {(entry['git'].get('commit') or '-')[:8]}"
          f"{' (미커밋 수정 있음)' if entry['git'].get('dirty') else ''}")
    if entry["status"] == "champion" and not a.dry_run:
        print("이미 champion입니다. 다시 재려면 --dry-run")
        reg.save()
        return 0

    champ = reg.champion()
    base = next((e["scores"] for e in reversed((champ or {}).get("evals", [])) if e.get("runner") == a.runner), None)
    left = f"champion {champ['id']}" if champ and base else "기준 결과(커밋된 파일)"
    if base is None:
        base = committed_scores(runner)
        if base is None:
            print("비교할 champion 평가도, 커밋된 기준 결과도 없습니다.")
            return 1

    if a.from_results:
        scores, kept = read_scores(a.from_results, runner["key"]), Path(a.from_results)
    else:
        others = busy()
        if others and not a.allow_busy:
            print("다른 평가·일괄 처리가 돌고 있어 시간 지표가 흐려집니다. 끝난 뒤 다시 실행하거나 --allow-busy:")
            print("\n".join("  " + o for o in others))
            return 2
        print("평가 실행:", " ".join(runner["cmd"]), flush=True)
        try:
            scores, kept = run_eval(runner, vid)
        except subprocess.CalledProcessError as e:
            # 평가가 끝까지 못 돌면 판정할 수 없다 — 거부와 구분해 '평가 실패'로 남기고 상태는 그대로 둔다
            reg.add_eval(vid, {"runner": a.runner, "at": datetime.now().isoformat(timespec="seconds"),
                               "verdict": "eval_failed", "error": f"종료 코드 {e.returncode}"})
            reg.save()
            print(f"\n평가 실패 (종료 코드 {e.returncode}) — 승격 판정 없음. 위 오류를 확인하고 다시 실행합니다.")
            return 1

    j = judge_promotion(base, scores, a.tolerance)
    print_table(j, left)
    for w in j["warnings"]:
        print("경고:", w)
    result = {"runner": a.runner, "at": datetime.now().isoformat(timespec="seconds"), "scores": scores,
              "models": comp["models"], "commit": entry["git"].get("commit"), "dirty": entry["git"].get("dirty"),
              "baseline": left, "verdict": j["verdict"], "drops": j["drops"], "warnings": j["warnings"],
              "file": str(kept)}
    if a.dry_run:
        print(f"\n[dry-run] 판정 {j['verdict']} — 레지스트리는 바꾸지 않았습니다.")
        return 0 if j["verdict"] == "pass" else 1
    reg.add_eval(vid, result)
    if j["verdict"] == "reject":
        reg.reject(vid, j["drops"])
        reg.save()
        print("\n승격 거부:", "; ".join(j["drops"]))
        return 1
    out = reg.promote(vid, note=f"{a.runner} 평가 통과 ({left} 대비)")
    reg.save()
    print(f"\n승격: {vid} → champion" + (f" · 이전 champion {out['retired']} → retired" if out["retired"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
