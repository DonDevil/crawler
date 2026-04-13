"""Rate limiting primitives used by the crawler."""

from __future__ import annotations

from aiolimiter import AsyncLimiter


class RateLimiter:
    """A simple rate limiter wrapper around aiolimiter.

    This can be used to enforce a global request rate across the crawler.
    """

    def __init__(self, max_calls: int, period: float) -> None:
        self._limiter = AsyncLimiter(max_calls, period)

    async def __aenter__(self):
        await self._limiter.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def acquire(self):
        """Acquire permission to perform an operation."""
        await self._limiter.acquire()
