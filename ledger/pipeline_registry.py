"""파이프라인 버전과 레지스트리 — 무엇이 바뀌면 다른 파이프라인인가 (docs/pipeline_ops.md §1).

버전 = 모델 이름 + 프롬프트 지문 + 임계값 + 주제·별칭 목록 지문 + 코드 지문.
git 커밋은 버전을 가르는 기준이 아니라 기록(되돌릴 때 찾아갈 곳)이다 — 문서만 고친 커밋마다 재평가하지 않게.
코드·프롬프트 지문은 AST로 잰다. 주석·docstring·줄 바꿈만 바뀐 것은 같은 버전이다.

레지스트리(store/pipeline_registry.json)는 버전마다 구성 요소·평가 결과·상태(champion / challenger / retired)를 둔다.
champion은 하나뿐이고, 승격 이력(lineage)을 남겨 롤백이 바로 앞 champion으로 돌아갈 수 있게 한다.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .store import STORE_DIR

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = STORE_DIR / "pipeline_registry.json"

# 동작을 정하는 코드 — 감시·관리 코드(metrics, monitor, access, 화면)는 버전에 넣지 않는다
CODE_FILES = ["agent/ledger_graph.py", "agent/enrich.py", "agent/retrieve.py", "agent/facts.py", "agent/crosscheck.py",
              "agent/schedule_add.py", "agent/retry.py", "ledger/resolve.py", "ledger/rag.py", "ledger/store.py",
              "ledger/records.py", "ledger/llm.py", "ledger/mcp_bridge.py", "ledger/schedule_parse.py",
              "mcp_ledger_server.py"]
# LLM에 지시문을 보내는 파일 — 프롬프트 지문은 이 파일들의 LLM 호출 인자와 MCP Prompt에서 뽑는다
PROMPT_FILES = ["agent/ledger_graph.py", "agent/enrich.py", "agent/retrieve.py", "agent/facts.py",
                "agent/schedule_add.py", "agent/crosscheck.py", "mcp_ledger_server.py"]
THRESHOLD_FILE = "ledger/thresholds.py"
# 주제 목록·용어집, 그리고 별칭 파일(이름에 alias가 들어간 JSON — 용어 색인 작업이 더할 예정)
VOCAB_FILES = ["data/project/topics.json", "data/project/r2_docs/용어집.md"]
VOCAB_GLOBS = ["data/project/**/*alias*.json", "data/project/**/*alias*.md"]

STATUSES = ("champion", "challenger", "retired")


def _now() -> str:
    """현재 시각 (초 단위 ISO 문자열)"""
    return datetime.now().isoformat(timespec="seconds")


def _sha(text: str, n: int = 12) -> str:
    """
    문자열 지문
    Args:
        text: 지문을 낼 문자열
        n:    남길 16진 글자 수
    Returns:
        sha256 앞 n자리
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """
    모듈·클래스·함수의 docstring을 떼어 낸다 (설명만 고친 것은 같은 코드로 보려고)
    Args:
        tree: 파싱한 AST (제자리에서 바꾼다)
    Returns:
        같은 AST
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return tree


def code_fingerprint(root: Path = ROOT) -> str:
    """
    동작 코드 지문 — 주석·docstring·서식 차이는 무시
    Args:
        root: 저장소 루트
    Returns:
        12자리 지문 (없는 파일은 '없음'으로 반영)
    """
    parts = []
    for rel in CODE_FILES:
        p = root / rel
        body = ast.dump(_strip_docstrings(ast.parse(p.read_text(encoding="utf-8")))) if p.exists() else "없음"
        parts.append(f"{rel}\n{body}")
    return _sha("\n".join(parts))


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """
    모듈 최상위 상수 정의 (이름 → 식 원문)
    Args:
        tree: 모듈 AST
    Returns:
        이름 → ast.unparse 결과
    """
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = ast.unparse(node.value)
    return out


def _own_nodes(fn: ast.AST):
    """
    함수 안의 노드를 차례로 — 안쪽에 정의한 함수는 따로 세므로 들어가지 않는다
    Args:
        fn: 함수 AST
    Returns:
        노드 생성기 (소스 순서)
    """
    stack = list(reversed(list(ast.iter_child_nodes(fn))))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def prompt_texts(root: Path = ROOT) -> dict[str, str]:
    """
    LLM 지시문 목록 — LLM 호출(.json·.text)의 시스템 문구·출력 형식, MCP Prompt 본문

    상수 이름으로 넘긴 문구는 그 상수의 정의를, 문장을 바로 쓴 호출은 그 식을 쓴다.
    키는 '파일:함수#순번' — 줄 번호를 쓰지 않아 다른 곳을 고쳐도 키가 그대로다.
    Args:
        root: 저장소 루트
    Returns:
        키 → 지시문 식 원문
    """
    out = {}
    for rel in PROMPT_FILES:
        p = root / rel
        if not p.exists():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        consts = _module_constants(tree)
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in funcs:
            # MCP Prompt: @mcp.prompt() 함수가 돌려주는 문구
            if any("prompt" in ast.unparse(d) for d in fn.decorator_list):
                rets = [ast.unparse(r.value) for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value]
                out[f"{rel}:{fn.name}"] = "\n".join(rets)
                continue
            n = 0
            for call in _own_nodes(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("json", "text") and call.args):
                    continue
                # 첫 인자가 지시문, json이면 세 번째가 출력 형식. 이름이면 상수 정의로 바꿔 넣는다
                args = [call.args[0]] + ([call.args[2]] if call.func.attr == "json" and len(call.args) > 2 else [])
                texts = [consts.get(a.id, a.id) if isinstance(a, ast.Name) else ast.unparse(a) for a in args]
                if isinstance(call.args[0], ast.Attribute) or texts[0] in ("system", "self"):
                    continue          # LLM 래퍼 자신의 구현(self.json 등)은 지시문이 아니다
                n += 1
                out[f"{rel}:{fn.name}#{n}"] = "\n".join(texts)
    return out


def thresholds(root: Path = ROOT) -> dict[str, float]:
    """
    임계값 파일의 숫자 상수 (파일을 직접 읽는다 — 롤백으로 고친 값이 바로 보이게)
    Args:
        root: 저장소 루트
    Returns:
        상수 이름 → 값
    """
    tree = ast.parse((root / THRESHOLD_FILE).read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
            out[node.targets[0].id] = node.value.value
    return out


def vocab_files(root: Path = ROOT) -> dict[str, str]:
    """
    주제·용어집·별칭 파일별 지문
    Args:
        root: 저장소 루트
    Returns:
        상대 경로 → 12자리 지문 (없는 파일은 빠진다)
    """
    paths = {root / f for f in VOCAB_FILES}
    for g in VOCAB_GLOBS:
        paths |= set(root.glob(g))
    return {str(p.relative_to(root)): _sha(p.read_text(encoding="utf-8")) for p in sorted(paths) if p.exists()}


def git_info(root: Path = ROOT) -> dict:
    """
    지금 코드의 git 커밋과 동작 코드에 커밋 안 된 수정이 있는지
    Args:
        root: 저장소 루트
    Returns:
        {"commit": 커밋 해시 또는 None, "dirty": 버전 구성 파일에 미커밋 수정이 있으면 True}
    """
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                                timeout=5, check=True).stdout.strip()
        files = [*CODE_FILES, THRESHOLD_FILE, *VOCAB_FILES]
        dirty = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=root, capture_output=True,
                               text=True, timeout=5, check=True).stdout.strip() != ""
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def env_checkers() -> list[str]:
    """
    환경변수로 켠 교차검증 모델 (agent/crosscheck.py와 같은 규칙: CROSSCHECK=1일 때 CROSSCHECK_MODELS)
    Returns:
        모델 이름 목록 ('ollama:' 붙임). 꺼져 있으면 빈 목록
    """
    if os.environ.get("CROSSCHECK", "0") != "1":
        return []
    names = os.environ.get("CROSSCHECK_MODELS", "qwen3:0.6b,qwen2.5:0.5b").split(",")
    return [f"ollama:{n.strip()}" for n in names if n.strip()]


def components(fast_model: str, quality_model: str, root: Path = ROOT, checkers: list[str] | None = None) -> dict:
    """
    버전을 이루는 구성 요소 계산
    Args:
        fast_model:    분류용 모델 이름 (예: ollama:qwen3:8b)
        quality_model: 작성용 모델 이름
        root:          저장소 루트
        checkers:      교차검증 모델 이름. None이면 환경변수(CROSSCHECK)로 정한다
    Returns:
        {models, prompt_fp, prompts(키별 지문), thresholds, vocab_fp, vocab(파일별 지문), code_fp}
    """
    prompts = {k: _sha(v, 8) for k, v in sorted(prompt_texts(root).items())}
    vocab = vocab_files(root)
    # 교차검증은 켜고 끄는 것만으로 판정이 달라진다 — 모델 묶음에 넣는다
    checks = env_checkers() if checkers is None else list(checkers)
    return {"models": {"fast": fast_model, "quality": quality_model, "checkers": checks},
            "prompt_fp": _sha(json.dumps(prompts, sort_keys=True)), "prompts": prompts,
            "thresholds": thresholds(root),
            "vocab_fp": _sha(json.dumps(vocab, sort_keys=True, ensure_ascii=False)), "vocab": vocab,
            "code_fp": code_fingerprint(root)}


def version_id(comp: dict) -> str:
    """
    구성 요소로 버전 ID를 만든다 (같은 구성이면 같은 ID)
    Args:
        comp: components() 결과
    Returns:
        'pv-' + 10자리 지문
    """
    key = {k: comp[k] for k in ("models", "prompt_fp", "thresholds", "vocab_fp", "code_fp")}
    return "pv-" + _sha(json.dumps(key, sort_keys=True, ensure_ascii=False), 10)


@lru_cache(maxsize=8)
def current(fast_model: str, quality_model: str, checkers: tuple[str, ...] | None = None) -> tuple[str, str]:
    """
    이 프로세스가 돌리는 버전 (모델 조합별로 한 번만 계산)

    파일은 프로세스가 도는 동안 바뀌지 않는다고 본다 — 임계값·프롬프트를 고치면 서버를 다시 띄운다.
    Args:
        fast_model:    분류용 모델 이름
        quality_model: 작성용 모델 이름
        checkers:      교차검증 모델 이름. None이면 환경변수로
    Returns:
        (버전 ID, 구성 요소 JSON 문자열) — lru_cache가 dict를 못 담아 문자열로 둔다
    """
    comp = components(fast_model, quality_model, checkers=None if checkers is None else list(checkers))
    return version_id(comp), json.dumps(comp, ensure_ascii=False)


def version_of(deps) -> str:
    """
    그래프 의존성(LedgerDeps)의 모델로 지금 버전 ID를 구한다
    Args:
        deps: fast_llm·quality_llm(.name)을 가진 객체
    Returns:
        버전 ID (모델 이름을 모르면 'unknown'으로 계산)
    """
    checkers = tuple(getattr(c, "name", "unknown") for c in (getattr(deps, "checkers", None) or []))
    return current(getattr(deps.fast_llm, "name", "unknown"), getattr(deps.quality_llm, "name", "unknown"),
                   checkers)[0]


def diff_components(a: dict, b: dict) -> dict:
    """
    두 버전의 구성 요소 차이 (롤백·승격 안내용)
    Args:
        a: 기준 버전 components
        b: 비교 버전 components
    Returns:
        {"models": 바뀐 역할, "thresholds": {이름: [a, b]}, "prompts": 바뀐 키, "vocab": 바뀐 파일, "code": bool}
    """
    def changed(x: dict, y: dict) -> list[str]:
        return sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))
    return {"models": changed(a["models"], b["models"]),
            "thresholds": {k: [a["thresholds"].get(k), b["thresholds"].get(k)]
                           for k in changed(a["thresholds"], b["thresholds"])},
            "prompts": changed(a.get("prompts", {}), b.get("prompts", {})),
            "vocab": changed(a.get("vocab", {}), b.get("vocab", {})),
            "code": a["code_fp"] != b["code_fp"]}


class Registry:
    """
    파이프라인 버전 레지스트리 (JSON 파일)

    구조: {"versions": {id: 항목}, "champion": id, "lineage": [역대 champion id], "log": [이력]}
    항목: {id, status, registered_at, components, git, evals:[평가 결과], promoted_at, retired_at, note}
    """

    def __init__(self, path: Path | str = REGISTRY):
        """
        레지스트리 파일 열기 (없으면 빈 레지스트리)
        Args:
            path: JSON 파일 경로
        """
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else \
            {"versions": {}, "champion": None, "lineage": [], "log": []}

    def save(self) -> None:
        """
        파일에 쓴다 — 임시 파일에 쓰고 바꿔 끼워 반쯤 쓴 파일이 남지 않게
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _log(self, action: str, version: str, **detail) -> None:
        """
        이력 한 줄 추가
        Args:
            action:  register | promote | reject | rollback | eval | policy_seen
            version: 대상 버전 ID
            detail:  추가 정보
        """
        self.data["log"].append({"at": _now(), "action": action, "version": version, **detail})

    def get(self, vid: str | None) -> dict | None:
        """
        버전 항목
        Args:
            vid: 버전 ID
        Returns:
            항목 또는 None
        """
        return self.data["versions"].get(vid) if vid else None

    def champion(self) -> dict | None:
        """
        지금 champion 항목
        Returns:
            항목 또는 None (아직 승격한 버전이 없음)
        """
        return self.get(self.data.get("champion"))

    def versions(self, status: str | None = None) -> list[dict]:
        """
        버전 목록 (등록 순)
        Args:
            status: 상태로 한정
        Returns:
            항목 목록
        """
        return [v for v in self.data["versions"].values() if status is None or v["status"] == status]

    def register(self, comp: dict, git: dict | None = None, note: str = "") -> dict:
        """
        버전 등록 — 이미 있으면 그대로 돌려준다 (새 버전은 challenger)
        Args:
            comp: components() 결과
            git:  git_info() 결과
            note: 메모
        Returns:
            항목
        """
        vid = version_id(comp)
        if vid in self.data["versions"]:
            return self.data["versions"][vid]
        entry = {"id": vid, "status": "challenger", "registered_at": _now(), "components": comp,
                 "git": git or {}, "evals": [], "promoted_at": None, "retired_at": None, "note": note}
        self.data["versions"][vid] = entry
        self._log("register", vid, commit=(git or {}).get("commit"))
        return entry

    def add_eval(self, vid: str, result: dict) -> None:
        """
        평가 결과 기록
        Args:
            vid:    버전 ID
            result: {runner, at, scores, ...}
        """
        self.data["versions"][vid]["evals"].append(result)
        self._log("eval", vid, runner=result.get("runner"))

    def promote(self, vid: str, note: str = "") -> dict:
        """
        champion 교체 — 이전 champion은 retired로 남긴다 (롤백 대상)
        Args:
            vid:  승격할 버전 ID
            note: 승격 사유
        Returns:
            {"champion": 새 champion, "retired": 이전 champion 또는 None}
        Raises:
            KeyError: 등록되지 않은 버전
        """
        if vid not in self.data["versions"]:
            raise KeyError(vid)
        old = self.data.get("champion")
        if old == vid:
            return {"champion": vid, "retired": None}
        if old:
            self.data["versions"][old].update(status="retired", retired_at=_now())
        self.data["versions"][vid].update(status="champion", promoted_at=_now(), note=note or "")
        self.data["champion"] = vid
        self.data["lineage"].append(vid)
        self._log("promote", vid, previous=old, note=note)
        return {"champion": vid, "retired": old}

    def reject(self, vid: str, reasons: list[str]) -> None:
        """
        승격 거부 기록 (상태는 challenger 그대로)
        Args:
            vid:     버전 ID
            reasons: 거부 사유
        """
        self._log("reject", vid, reasons=reasons)

    def rollback(self) -> dict:
        """
        바로 앞 champion으로 되돌린다 — 지금 champion은 retired
        Returns:
            {"champion": 되돌아간 버전, "retired": 내려간 버전}
        Raises:
            ValueError: 되돌아갈 이전 champion이 없음
        """
        lineage = self.data["lineage"]
        if len(lineage) < 2:
            raise ValueError("되돌아갈 이전 champion이 없습니다.")
        cur, prev = lineage.pop(), lineage[-1]
        self.data["versions"][cur].update(status="retired", retired_at=_now(), note="롤백으로 내려감")
        self.data["versions"][prev].update(status="champion", promoted_at=_now())
        self.data["champion"] = prev
        self._log("rollback", prev, previous=cur)
        return {"champion": prev, "retired": cur}

    def promoted_at(self, vid: str) -> str | None:
        """
        버전이 마지막으로 champion이 된 시각 (기준선 창의 시작)
        Args:
            vid: 버전 ID
        Returns:
            ISO 시각 또는 None
        """
        v = self.get(vid)
        return v.get("promoted_at") if v else None


def register_current(fast_model: str, quality_model: str, path: Path | str = REGISTRY) -> dict:
    """
    지금 버전을 계산해 레지스트리에 없으면 challenger로 등록 (서버 시작 때)
    Args:
        fast_model:    분류용 모델 이름
        quality_model: 작성용 모델 이름
        path:          레지스트리 경로
    Returns:
        항목 (status로 champion 여부를 본다)
    """
    vid, comp = current(fast_model, quality_model)
    reg = Registry(path)
    entry = reg.get(vid)
    if entry is None:
        entry = reg.register(json.loads(comp), git_info(), note="서버 시작 때 등록")
        reg.save()
    return entry
