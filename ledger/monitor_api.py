"""품질 감시 API와 서버 연결부 (FastAPI 라우터) — docs/pipeline_ops.md.

- GET /admin/monitor : 파이프라인 버전·드리프트 판정·정책 개정 (팀장 전용, 관리자 콘솔 '품질 감시' 탭)
- setup()            : 서버 시작 때 지금 버전을 계산해 레지스트리에 없으면 challenger로 등록
- record()           : 요청이 끝날 때 지표 1행 기록. 실패해도 답변은 막지 않는다
- link_decision()    : 그래프 밖에서 원장에 올린 결정(의견 → 결정으로 올리기)의 버전 기록

server.py가 include_router로 붙이고, 상태는 app.state(metrics, versions, access)에 둔다.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from . import monitor, pipeline_registry
from .metrics import MetricsStore, extract
from .records import MeetingRecords
from .store import LedgerStore

router = APIRouter()


def setup(state, fast_name: str, quality_name: str, presets: dict[str, tuple[str, str, str]]) -> None:
    """
    서버 시작 때 버전 등록과 지표 저장소 준비
    Args:
        state:        app.state (metrics, versions를 채운다)
        fast_name:    기본 그래프의 분류 모델 이름
        quality_name: 기본 그래프의 작성 모델 이름
        presets:      server.PRESETS (조합 키 → (이름, fast 모델, quality 모델)) — 조합마다 버전이 다르다
    """
    state.metrics = MetricsStore()
    entry = pipeline_registry.register_current(fast_name, quality_name)
    state.versions = {"accurate": entry["id"]}
    for key, (_, f, q) in presets.items():
        state.versions.setdefault(key, pipeline_registry.current(f"ollama:{f}", f"ollama:{q}")[0])
    reg = pipeline_registry.Registry()
    if not reg.data.get("policy_seen"):          # 정책 개정 감지의 출발점 — 처음 본 정책 버전을 기억한다
        reg.data["policy_seen"] = LedgerStore().current_policy_version()
        reg.save()


def run_config(state, model: str = "accurate") -> dict:
    """
    그래프 실행 설정 — LangSmith 추적에 파이프라인 버전을 메타데이터로 남긴다
    Args:
        state: app.state
        model: 모델 조합 키
    Returns:
        astream·ainvoke의 config 인자
    """
    versions = getattr(state, "versions", {}) or {}
    return {"metadata": {"pipeline_version": versions.get(model) or versions.get("accurate", "unknown"),
                         "model_preset": model}}


def record(state, result: dict, t0: float, user: str, model: str = "accurate", cached: bool = False,
           error: str | None = None) -> None:
    """
    요청 1건의 지표를 store/metrics.db에 남긴다 (감시는 부가 기능이라 예외는 삼킨다)
    Args:
        state:  app.state
        result: 그래프 최종 상태 (캐시 적중이면 저장해 둔 mode·events)
        t0:     요청 시작 시각 (time.perf_counter)
        user:   사용자 ID
        model:  모델 조합 키 (PRESETS)
        cached: 저장된 답을 돌려준 요청
        error:  실행 중 오류 문구
    """
    try:
        ver = state.versions.get(model) or state.versions["accurate"]
        state.metrics.record(extract(result, time.perf_counter() - t0, ver, "chat", user, model, cached, error))
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def link_decision(state, decision_id: str) -> None:
    """
    그래프 밖에서 올린 결정의 버전 기록 (사람 신호를 버전에 묶으려고)
    Args:
        state:       app.state
        decision_id: 원장 결정 ID
    """
    try:
        state.metrics.link_decision(decision_id, state.versions["accurate"])
    except Exception:  # pylint: disable=broad-exception-caught
        pass


@router.get("/admin/monitor")
async def admin_monitor(request: Request, user: str):
    """
    품질 감시 — 기준선 창(champion 승격 직후 N건)과 최근 창의 입력·동작·사람 지표 판정, 버전 목록, 정책 개정 (팀장 전용)
    Args:
        request: 요청 (app.state 접근)
        user:    요청한 사용자 ID
    Returns:
        monitor.report 결과 (running, champion, window, findings, alerts, warnings, human, versions, policy)
    Raises:
        HTTPException: 403 — 팀장이 아님
    """
    st = request.app.state
    if not st.access.is_lead(user):
        raise HTTPException(403, "팀장만 할 수 있습니다.")
    return monitor.report(st.metrics, MeetingRecords(), pipeline_registry.Registry(), LedgerStore(),
                          running=st.versions["accurate"])
