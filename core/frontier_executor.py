"""Non-blocking execution boundary between asyncio crawler code and the
synchronous `Frontier` protocol (core/frontier.py).

The `Frontier` protocol is deliberately synchronous (see
docs/architecture/frontier-adr.md) so the local in-memory frontier and the
Redis-backed frontier can share one interface without the protocol itself
knowing which backend is in play. `URLFrontier` (core/url_frontier.py) is
pure in-memory work and safe to call directly from an event loop.
`RedisURLFrontier` (core/redis_frontier.py) performs blocking network I/O
through a synchronous `redis-py` client and must never run directly on the
event loop thread -- see docs/architecture/frontier-step4.md for the
verification that motivated this module.

`AsyncFrontier` wraps any `Frontier`-conforming object and exposes `async`
methods that either call straight through (local frontier -- no thread,
no overhead) or offload to the event loop's shared default thread pool via
`asyncio.to_thread` (anything else -- Redis today, any future networked
backend automatically, safe-by-default rather than requiring each new
backend to opt in). `asyncio.to_thread` reuses `loop.run_in_executor` with
the loop's default `ThreadPoolExecutor`, which is a bounded, shared pool
(not a thread spawned per call), so this never grows unbounded with worker
concurrency.

This module does not change the `Frontier` protocol -- it stays fully
synchronous. `AsyncFrontier` is a separate, optional adapter that crawler
code opts into at construction time.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

from core.frontier import Frontier, FrontierClaim
from core.url_frontier import URLFrontier

T = TypeVar("T")


class AsyncFrontier:
    """Async-safe adapter around a synchronous `Frontier` implementation.

    Every method here has a 1:1 synchronous counterpart on `Frontier`
    (core/frontier.py) plus `reclaim_and_promote`, which only Redis-backed
    frontiers implement (a no-op `(0, 0)` for backends that don't).
    """

    def __init__(self, frontier: Frontier | "AsyncFrontier"):
        if isinstance(frontier, AsyncFrontier):
            # Idempotent: wrapping an already-wrapped frontier reuses the
            # same underlying object and offload decision instead of
            # nesting adapters (which would hand a coroutine function to
            # asyncio.to_thread and silently do the wrong thing).
            self._frontier: Frontier = frontier._frontier
            self._offload = frontier._offload
            return

        self._frontier = frontier
        # Only the in-memory/SQLite frontier is guaranteed not to block on
        # network I/O; every other implementation (Redis, and any future
        # backend) is offloaded to a thread by default.
        self._offload = not isinstance(frontier, URLFrontier)

    @property
    def raw(self) -> Frontier:
        """Escape hatch to the wrapped synchronous frontier (e.g. for
        attribute access that isn't a frontier operation)."""
        return self._frontier

    async def _run(self, func: Callable[..., T], *args: Any) -> T:
        if not self._offload:
            return func(*args)
        return await asyncio.to_thread(func, *args)

    async def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool:
        return await self._run(self._frontier.add_url, url, priority, source_query)

    async def get_next_url(self) -> FrontierClaim | None:
        return await self._run(self._frontier.get_next_url)

    async def renew_claim(self, claim: FrontierClaim) -> FrontierClaim | None:
        return await self._run(self._frontier.renew_claim, claim)

    async def mark_visited(self, claim: FrontierClaim) -> None:
        await self._run(self._frontier.mark_visited, claim)

    async def mark_failed(self, claim: FrontierClaim, error: str = "") -> None:
        await self._run(self._frontier.mark_failed, claim, error)

    async def mark_skipped(self, claim: FrontierClaim) -> None:
        await self._run(self._frontier.mark_skipped, claim)

    async def has_pending(self) -> bool:
        return await self._run(self._frontier.has_pending)

    async def pending_count(self) -> int:
        return await self._run(self._frontier.pending_count)

    async def get_source_query(self, url: str) -> str:
        return await self._run(self._frontier.get_source_query, url)

    async def get_status_counts(self) -> dict[str, int]:
        return await self._run(self._frontier.get_status_counts)

    async def reclaim_and_promote(self, batch_size: int | None = None) -> tuple[int, int]:
        """Reclaim abandoned inflight claims and promote due retries.

        Only Redis-backed frontiers implement this (there is nothing to
        reclaim in-process for the local frontier -- see ADR §7/§10); a
        frontier without it is treated as a no-op sweep.
        """
        method = getattr(self._frontier, "reclaim_and_promote", None)
        if method is None:
            return (0, 0)
        return await self._run(method, batch_size)

    async def close(self) -> None:
        method = getattr(self._frontier, "close", None)
        if method is None:
            return
        await self._run(method)
