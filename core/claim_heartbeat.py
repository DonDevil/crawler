"""Shared claim-heartbeat/renewal mechanism for legitimately long-running work.

See docs/architecture/frontier-adr.md §8 and docs/architecture/frontier-step5.md.

Problem: `lease_ttl` (docs/architecture/frontier-adr.md §4) has to stay short so a
genuinely crashed worker's claim is reclaimed quickly by the Step 4 recovery task,
but some fetches (slow Tor circuits, JS-heavy pages, deliberately throttled targets)
legitimately run longer than any single short lease. `run_with_heartbeat` decouples
the two: a live worker proves it's still alive by periodically calling
`renew_claim`, so the lease can stay short without falsely expiring slow-but-alive
work.

This module does not talk to Redis (or any frontier backend) directly -- it takes
an `AsyncFrontier` (core/frontier_executor.py), so a Redis-backed renewal is
automatically routed through that adapter's `asyncio.to_thread` offload and never
runs on the event-loop thread. It does not change the synchronous `Frontier`
protocol (core/frontier.py) or the Redis keyspace/Lua scripts.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Optional, TypeVar

from core.frontier import FrontierClaim

if TYPE_CHECKING:
    from core.frontier_executor import AsyncFrontier

T = TypeVar("T")

# Mirrors URLFrontier's/RedisURLFrontier's own constructor default -- used only
# as a fallback when a frontier instance doesn't expose `.lease_ttl` (e.g. a
# test double), so heartbeat timing degrades gracefully rather than crashing.
DEFAULT_LEASE_TTL = 90.0

# Absolute floor applied everywhere an interval is computed or accepted, so a
# pathological value (0, negative, float rounding) can't produce a busy-loop
# of renewal calls. Deliberately small (not e.g. 1.0s): it must never be the
# thing that decides real heartbeat cadence -- for a short lease_ttl (small
# in production only if an operator chose it, routinely small in tests), a
# 1-second-scale floor would silently force the interval past the lease
# itself, defeating the whole mechanism. `default_heartbeat_interval` is
# where a production-sane default lives; this constant only guards against
# degenerate input.
_MIN_HEARTBEAT_INTERVAL = 0.05


class ClaimLostError(Exception):
    """Raised by `run_with_heartbeat` when the frontier reports that `claim`
    is no longer current -- its lease already lapsed and was reclaimed (by
    the Step 4 recovery task or another worker), or it was otherwise
    invalidated.

    Whoever catches this must not call `mark_visited`/`mark_failed`/
    `mark_skipped` with the original claim: it is no longer this worker's to
    resolve, and doing so would either be a silently-ignored no-op (stale
    token) or, worse, corrupt whichever newer claim now owns the URL.
    """

    def __init__(self, claim: FrontierClaim):
        self.claim = claim
        super().__init__(f"claim lost for {claim.url!r} (token={claim.token})")


def default_heartbeat_interval(lease_ttl: float = DEFAULT_LEASE_TTL) -> float:
    """Derive a safe renewal cadence from `lease_ttl`.

    Renews at 1/3 of the lease window, so a live worker gets two renewal
    attempts of margin before its lease would actually lapse -- one missed or
    slow tick (network hiccup, scheduling jitter) still leaves time for the
    next one to land before expiry.
    """
    if lease_ttl is None or lease_ttl <= 0:
        lease_ttl = DEFAULT_LEASE_TTL
    return max(_MIN_HEARTBEAT_INTERVAL, lease_ttl / 3.0)


def resolve_heartbeat_interval(configured: Optional[float], lease_ttl: float) -> float:
    """Resolve the effective heartbeat interval for a frontier whose lease is
    `lease_ttl` seconds long.

    `configured` is an optional explicit override (`FrontierConfig.heartbeat_interval`
    or a crawler backend's constructor argument). When absent, the safe default
    (`default_heartbeat_interval`) is used. When present, it is still clamped
    below `lease_ttl` -- an explicit value that meets or exceeds the lease would
    guarantee the lease expires before the first renewal ever fires, defeating
    the whole mechanism, so that misconfiguration is corrected rather than
    honored.
    """
    if lease_ttl is None or lease_ttl <= 0:
        lease_ttl = DEFAULT_LEASE_TTL

    if configured is None or configured <= 0:
        return default_heartbeat_interval(lease_ttl)

    safe_ceiling = max(_MIN_HEARTBEAT_INTERVAL, lease_ttl / 2.0)
    return min(configured, safe_ceiling)


async def _cancel_and_drain(task: "asyncio.Task") -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except BaseException:
        # We're already unwinding (claim lost / outer cancellation) -- the
        # point of draining is only to avoid an orphaned task and a
        # "Task exception was never retrieved" warning, not to surface
        # whatever the cancelled work raised.
        pass


async def run_with_heartbeat(
    frontier: "AsyncFrontier",
    claim: FrontierClaim,
    coro: Awaitable[T],
    heartbeat_interval: float,
) -> tuple[T, FrontierClaim]:
    """Run `coro` to completion while periodically renewing `claim`'s lease.

    Starts heartbeating immediately (the caller must only invoke this once a
    claim actually exists) and stops as soon as `coro` finishes -- success,
    exception, or cancellation -- or as soon as a renewal reports the claim
    is no longer current. Never leaves the wrapped work running past this
    call's return: every exit path cancels and drains it first.

    Returns `(result, claim)` where `claim` reflects the most recent
    successful renewal (fresh `lease_expires_at`); callers that don't need
    the updated claim can ignore the second element.

    Raises whatever `coro` raised, `asyncio.CancelledError` if this call
    itself was cancelled, or `ClaimLostError` if `frontier.renew_claim`
    reported the claim as stale/reclaimed.
    """
    interval = max(_MIN_HEARTBEAT_INTERVAL, heartbeat_interval)
    task: "asyncio.Task[T]" = asyncio.ensure_future(coro)

    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=interval)
            if task in done:
                return task.result(), claim

            renewed = await frontier.renew_claim(claim)
            if renewed is None:
                raise ClaimLostError(claim)
            claim = renewed
    except BaseException:
        # Covers ClaimLostError, this call being cancelled, and any
        # unexpected error out of renew_claim itself -- on every exit path
        # other than a clean `coro` completion, make sure the wrapped work
        # is cancelled and drained before the exception propagates, so it
        # never keeps running past this function's return (requirement:
        # no orphan heartbeat/work task survives worker exit).
        await _cancel_and_drain(task)
        raise
