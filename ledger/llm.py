"""로컬 Ollama LLM 클라이언트 — JSON 스키마 강제 출력, 사용량 집계.

회의록·면접 기록은 기밀이라 외부 API로 보내지 않는다(정책 SEC-01과 같은 이유).
qwen3는 기본으로 긴 thinking을 출력해서 느리다. 분류·추출은 think=False, 판단이 중요한
단계만 think=True로 켤 수 있게 했다.
"""
from __future__ import annotations

import json
import os
import time

import httpx
from langsmith import traceable

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")   # 모델을 메모리에 붙잡아 둬서 질문마다 다시 올리는 시간(수 초)을 없앤다
NUM_CTX = int(os.environ.get("NUM_CTX", "16384"))   # 기본 4096이면 회의록+용어집 프롬프트가 잘린다


class LocalLLM:
    """
    로컬 Ollama 모델 클라이언트. JSON 스키마 강제 호출과 스트리밍 텍스트 호출 제공

    Attributes:
        model:  Ollama 모델 이름 (예: qwen3:8b)
        name:  사용량 기록용 이름 ("ollama:<model>")
        label:  티어 표시용 라벨 (fast / quality)
        usd_per_1k:  비교용 가상 단가
        calls:  누적 호출 수
        tokens:  누적 토큰 수 (프롬프트 + 생성)
    """

    def __init__(self, model: str, usd_per_1k_tokens: float = 0.0, label: str = ""):
        """
        모델 정보와 사용량 카운터 초기화
        Args:
            model:  Ollama 모델 이름
            usd_per_1k_tokens:  1천 토큰당 가상 단가(USD)
            label:  티어 라벨. 비우면 모델 이름 사용
        """
        self.model = model
        self.name = f"ollama:{model}"
        self.label = label or model
        self.usd_per_1k = usd_per_1k_tokens   # 로컬은 실비용 0 — 비교용 가상 단가
        self.calls = 0
        self.tokens = 0

    @traceable(run_type="llm", name="local_llm.json")
    async def json(self, system: str, user: str, schema: dict, think: bool = False,  # pylint: disable=too-many-arguments
                   timeout: float = 300.0, max_tokens: int = 2000, num_ctx: int | None = None) -> tuple[dict, dict]:
        """
        JSON 스키마를 강제한 응답을 받아 dict로 반환

        Ollama format에 스키마를 넘겨 출력 구조를 고정하고, temperature 0으로 결과를 재현 가능하게 둔다.
        Args:
            system:  시스템 프롬프트
            user:  사용자 프롬프트
            schema:  출력 JSON 스키마
            think:  qwen3 thinking 사용 여부 (판단이 중요한 단계만 True)
            timeout:  HTTP 타임아웃(초)
            max_tokens:  생성 토큰 상한 (num_predict)
            num_ctx:  컨텍스트 길이. None이면 기본값(NUM_CTX). 짧은 판정용 작은 모델은 줄여서 메모리를 아낀다
        Returns:
            (파싱된 결과, 사용량) 튜플. 파싱 실패 시 결과는 {}이고 사용량에 parse_error=True
        Raises:
            httpx.HTTPStatusError: Ollama가 오류 상태 코드를 돌려줄 때
        """
        # max_tokens: 작은 모델이 같은 항목을 끝없이 반복하는 경우를 끊는다 (잘리면 빈 결과로 처리)
        payload = {"model": self.model, "stream": False, "think": think, "format": schema, "keep_alive": KEEP_ALIVE,
                   "options": {"temperature": 0.0, "num_ctx": num_ctx or NUM_CTX, "num_predict": max_tokens},
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        content = data["message"]["content"]
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        self.calls += 1
        self.tokens += tokens
        usage = {"model": self.name, "tokens": tokens, "seconds": round(time.perf_counter() - t0, 2),
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        try:
            return json.loads(content), usage
        except json.JSONDecodeError:
            # 스키마 강제여도 드물게 잘림 — 빈 결과로 돌려 상위 노드가 재시도/폴백하게 함
            return {}, {**usage, "parse_error": True}

    @traceable(run_type="llm", name="local_llm.text")
    async def text(self, system: str, user: str, on_token=None, timeout: float = 600.0,
                   max_tokens: int = 800) -> tuple[str, dict]:
        """
        자유 텍스트 응답을 스트리밍으로 받아 이어 붙여 반환

        답변 작성 단계용이라 thinking은 끈다. 토큰이 올 때마다 on_token으로 화면에 바로 흘려보낸다.
        Args:
            system:  시스템 프롬프트
            user:  사용자 프롬프트
            on_token:  조각 문자열을 받는 콜백 (없으면 생략)
            timeout:  HTTP 타임아웃(초)
            max_tokens:  생성 토큰 상한 (num_predict)
        Returns:
            (전체 텍스트, 사용량) 튜플
        Raises:
            httpx.HTTPStatusError: Ollama가 오류 상태 코드를 돌려줄 때
        """
        payload = {"model": self.model, "stream": True, "think": False, "keep_alive": KEEP_ALIVE,
                   "options": {"temperature": 0.0, "num_ctx": NUM_CTX, "num_predict": max_tokens},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        chunks, tokens = [], 0
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as r:
                r.raise_for_status()
                # Ollama 스트림은 줄마다 JSON 한 개(NDJSON). 토큰 수는 done=true인 마지막 줄에만 실림
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    piece = d.get("message", {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        if on_token:
                            on_token(piece)
                    if d.get("done"):
                        tokens = (d.get("prompt_eval_count") or 0) + (d.get("eval_count") or 0)
        self.calls += 1
        self.tokens += tokens
        return "".join(chunks), {"model": self.name, "tokens": tokens,
                                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


async def warm_up(models: list[str]) -> None:
    """
    서버 시작 때 모델을 미리 메모리에 올림

    첫 질문이 모델 로딩 시간만큼 느려지는 것 방지. prompt 없이 /api/generate를 부르면 로딩만 수행.
    Args:
        models:  올릴 모델 이름 목록 (중복은 한 번만)
    """
    async with httpx.AsyncClient(timeout=180) as c:
        # dict.fromkeys: 순서를 유지한 채 중복 제거 (fast·quality가 같은 모델일 때 한 번만)
        for m in dict.fromkeys(models):
            # 예열은 최적화일 뿐이라 Ollama가 꺼져 있어도 서버 시작을 막지 않음
            try:
                await c.post(f"{OLLAMA_URL}/api/generate", json={"model": m, "keep_alive": KEEP_ALIVE})
            except httpx.HTTPError:
                pass


class OpenAILLM:
    """
    OpenAI Chat Completions 클라이언트. LocalLLM과 같은 인터페이스(json/text)

    LLM_BACKEND=openai — Ollama가 느린 맥북용 대안 (강의 안내 3-2).
    실제 과금되는 원격 API다. 회의록이 외부로 나가므로 사내 실데이터에는 쓰지 않는다 (SEC-01).

    Attributes:
        URL:  Chat Completions 엔드포인트
        model:  OpenAI 모델 이름
        name:  사용량 기록용 이름 ("openai:<model>")
        label:  티어 라벨 (fast / quality)
        usd_per_1k:  1천 토큰당 단가
        calls:  누적 호출 수
        tokens:  누적 토큰 수
        headers:  OPENAI_API_KEY로 만든 인증 헤더
    """
    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str, usd_per_1k_tokens: float = 0.0, label: str = ""):
        """
        모델 정보·사용량 카운터·인증 헤더 초기화
        Args:
            model:  OpenAI 모델 이름
            usd_per_1k_tokens:  1천 토큰당 단가(USD)
            label:  티어 라벨. 비우면 모델 이름 사용
        """
        self.model, self.name, self.label = model, f"openai:{model}", label or model
        self.usd_per_1k, self.calls, self.tokens = usd_per_1k_tokens, 0, 0
        self.headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}

    @traceable(run_type="llm", name="openai_llm.json")
    async def json(self, system: str, user: str, schema: dict, think: bool = False,  # pylint: disable=unused-argument  # think는 LocalLLM과 인터페이스를 맞추려고 받기만 한다
                   timeout: float = 120.0, max_tokens: int = 2000) -> tuple[dict, dict]:
        """
        response_format json_schema로 구조를 강제한 응답을 받아 dict로 반환
        Args:
            system:  시스템 프롬프트
            user:  사용자 프롬프트
            schema:  출력 JSON 스키마
            think:  LocalLLM과 인터페이스를 맞추기 위한 인자 (사용하지 않음)
            timeout:  HTTP 타임아웃(초)
            max_tokens:  생성 토큰 상한
        Returns:
            (파싱된 결과, 사용량) 튜플. 파싱 실패 시 결과는 {}이고 사용량에 parse_error=True
        Raises:
            httpx.HTTPStatusError: API가 오류 상태 코드를 돌려줄 때
        """
        payload = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                   "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(self.URL, json=payload, headers=self.headers)
            r.raise_for_status()
            data = r.json()
        tokens = data.get("usage", {}).get("total_tokens", 0)
        self.calls += 1
        self.tokens += tokens
        usage = {"model": self.name, "tokens": tokens, "seconds": round(time.perf_counter() - t0, 2),
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        # 응답 구조가 어긋나거나 잘린 경우도 LocalLLM과 같이 빈 결과로 돌려 상위 노드가 폴백
        try:
            return json.loads(data["choices"][0]["message"]["content"]), usage
        except (json.JSONDecodeError, KeyError, IndexError):
            return {}, {**usage, "parse_error": True}

    @traceable(run_type="llm", name="openai_llm.text")
    async def text(self, system: str, user: str, on_token=None, timeout: float = 120.0,
                   max_tokens: int = 800) -> tuple[str, dict]:
        """
        자유 텍스트 응답을 SSE 스트리밍으로 받아 이어 붙여 반환
        Args:
            system:  시스템 프롬프트
            user:  사용자 프롬프트
            on_token:  조각 문자열을 받는 콜백 (없으면 생략)
            timeout:  HTTP 타임아웃(초)
            max_tokens:  생성 토큰 상한
        Returns:
            (전체 텍스트, 사용량) 튜플
        Raises:
            httpx.HTTPStatusError: API가 오류 상태 코드를 돌려줄 때
        """
        # include_usage: 스트리밍은 기본으로 토큰 수를 주지 않아 마지막 청크에 usage를 받도록 요청
        payload = {"model": self.model, "temperature": 0, "stream": True, "max_tokens": max_tokens,
                   "stream_options": {"include_usage": True},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        chunks, tokens = [], 0
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", self.URL, json=payload, headers=self.headers) as r:
                r.raise_for_status()
                # SSE 형식: 데이터 줄은 "data: " 접두어, 끝은 "data: [DONE]". 빈 줄·종료 표시는 건너뜀
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    d = json.loads(line[6:])
                    if d.get("usage"):
                        tokens = d["usage"].get("total_tokens", 0)
                    for ch in d.get("choices", []):
                        piece = ch.get("delta", {}).get("content") or ""
                        if piece:
                            chunks.append(piece)
                            if on_token:
                                on_token(piece)
        self.calls += 1
        self.tokens += tokens
        return "".join(chunks), {"model": self.name, "tokens": tokens, "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


def build_llms():
    """
    환경변수에 따라 fast·quality 두 티어의 LLM 클라이언트 생성

    fast(분류·주제 선택)와 quality(추출·충돌 판정·작성) — 모델 티어링.
    LLM_BACKEND=ollama(기본, 로컬) | openai(과금, Ollama가 느린 환경용). 임베딩은 어느 쪽이든 로컬 Ollama.
    Returns:
        (fast, quality) 클라이언트 튜플. LocalLLM 또는 OpenAILLM
    """
    if os.environ.get("LLM_BACKEND", "ollama") == "openai":
        return (OpenAILLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"), 0.00015, "fast"),
                OpenAILLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o-mini"), 0.00015, "quality"))
    fast = LocalLLM(os.environ.get("FAST_MODEL", "qwen3:8b"), 0.00015, "fast")
    quality = LocalLLM(os.environ.get("QUALITY_MODEL", "qwen3:8b"), 0.005, "quality")
    return fast, quality
