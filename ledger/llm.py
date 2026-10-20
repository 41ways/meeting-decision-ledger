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
    def __init__(self, model: str, usd_per_1k_tokens: float = 0.0, label: str = ""):
        self.model = model
        self.name = f"ollama:{model}"
        self.label = label or model
        self.usd_per_1k = usd_per_1k_tokens   # 로컬은 실비용 0 — 비교용 가상 단가
        self.calls = 0
        self.tokens = 0

    @traceable(run_type="llm", name="local_llm.json")
    async def json(self, system: str, user: str, schema: dict, think: bool = False,
                   timeout: float = 300.0, max_tokens: int = 2000) -> tuple[dict, dict]:
        # max_tokens: 작은 모델이 같은 항목을 끝없이 반복하는 경우를 끊는다 (잘리면 빈 결과로 처리)
        payload = {"model": self.model, "stream": False, "think": think, "format": schema, "keep_alive": KEEP_ALIVE,
                   "options": {"temperature": 0.0, "num_ctx": NUM_CTX, "num_predict": max_tokens},
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
    async def text(self, system: str, user: str, on_token=None, timeout: float = 600.0, max_tokens: int = 800) -> tuple[str, dict]:
        payload = {"model": self.model, "stream": True, "think": False, "keep_alive": KEEP_ALIVE, "options": {"temperature": 0.0, "num_ctx": NUM_CTX, "num_predict": max_tokens},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        chunks, tokens = [], 0
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as r:
                r.raise_for_status()
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
    """서버 시작 때 모델을 미리 메모리에 올린다 (첫 질문이 모델 로딩 시간만큼 느려지는 것 방지)."""
    async with httpx.AsyncClient(timeout=180) as c:
        for m in dict.fromkeys(models):
            try:
                await c.post(f"{OLLAMA_URL}/api/generate", json={"model": m, "keep_alive": KEEP_ALIVE})
            except Exception:
                pass


class OpenAILLM:
    """LLM_BACKEND=openai — Ollama가 느린 맥북용 대안 (강의 안내 3-2). 같은 인터페이스(json/text).
    실제 과금되는 원격 API다. 회의록이 외부로 나가므로 사내 실데이터에는 쓰지 않는다 (SEC-01)."""
    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str, usd_per_1k_tokens: float = 0.0, label: str = ""):
        self.model, self.name, self.label = model, f"openai:{model}", label or model
        self.usd_per_1k, self.calls, self.tokens = usd_per_1k_tokens, 0, 0
        self.headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}

    @traceable(run_type="llm", name="openai_llm.json")
    async def json(self, system: str, user: str, schema: dict, think: bool = False,
                   timeout: float = 120.0, max_tokens: int = 2000) -> tuple[dict, dict]:
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
        try:
            return json.loads(data["choices"][0]["message"]["content"]), usage
        except (json.JSONDecodeError, KeyError, IndexError):
            return {}, {**usage, "parse_error": True}

    @traceable(run_type="llm", name="openai_llm.text")
    async def text(self, system: str, user: str, on_token=None, timeout: float = 120.0, max_tokens: int = 800) -> tuple[str, dict]:
        payload = {"model": self.model, "temperature": 0, "stream": True, "max_tokens": max_tokens, "stream_options": {"include_usage": True},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        chunks, tokens = [], 0
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", self.URL, json=payload, headers=self.headers) as r:
                r.raise_for_status()
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
    """fast(분류·주제 선택)와 quality(추출·충돌 판정·작성) — 모델 티어링.
    LLM_BACKEND=ollama(기본, 로컬) | openai(과금, Ollama가 느린 환경용). 임베딩은 어느 쪽이든 로컬 Ollama."""
    if os.environ.get("LLM_BACKEND", "ollama") == "openai":
        return (OpenAILLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"), 0.00015, "fast"),
                OpenAILLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o-mini"), 0.00015, "quality"))
    fast = LocalLLM(os.environ.get("FAST_MODEL", "qwen3:8b"), 0.00015, "fast")
    quality = LocalLLM(os.environ.get("QUALITY_MODEL", "qwen3:8b"), 0.005, "quality")
    return fast, quality
