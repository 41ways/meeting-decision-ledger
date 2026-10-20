"""Wave 9 — Transient 에러만 exponential backoff로 재시도.

asyncio.TimeoutError(일시적)만 재시도하고, 그 외(ValueError 등 영구적)는 즉시 전파한다.
"""
import asyncio

from langsmith import traceable


@traceable(name="with_retry")
async def with_retry(fn, *args, retries: int = 3, base_delay: float = 0.3, **kwargs):
    """
    비동기 함수를 호출하고 타임아웃일 때만 지수 백오프로 재시도

    대기 시간은 base_delay × 3^(n-1) (기본값 기준 0.3초 → 0.9초).
    Args:
        fn:  호출할 비동기 함수
        *args:  fn에 넘길 위치 인자
        retries:  최대 시도 횟수 (첫 시도 포함)
        base_delay:  첫 재시도 전 대기 시간(초)
        **kwargs:  fn에 넘길 키워드 인자
    Returns:
        (fn의 결과, 재시도 횟수) 튜플
    Raises:
        asyncio.TimeoutError: retries번 모두 타임아웃일 때
        Exception: 타임아웃 이외의 예외는 재시도 없이 그대로 전파
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await fn(*args, **kwargs)
            return result, attempt - 1   # (결과, 재시도 횟수) — 첫 시도 성공이면 0
        except asyncio.TimeoutError:
            # 타임아웃만 일시 오류로 본다. 입력 오류 같은 영구 오류는 다시 해도 같은 결과라 잡지 않음
            if attempt >= retries:
                raise
            delay = base_delay * (3 ** (attempt - 1))
            await asyncio.sleep(delay)
