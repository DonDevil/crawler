"""Common frontier contract shared by every frontier backend (local, Redis, ...).

See docs/architecture/frontier-adr.md for the design this codifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class FrontierUnavailable(Exception):
    """A frontier operation could not be completed because of an
    infrastructure failure in the backend (e.g. a Redis connection or
    timeout error), not because of the frontier's actual URL state.

    This is the sole signal backends use to distinguish "the operation
    legitimately found nothing / had nothing to do" from "the operation
    could not be performed at all." Callers must never fold this into an
    ordinary return value (`False`, `0`, `None`, an empty dict/list) --
    doing so is exactly the bug this exception exists to prevent (see
    docs/architecture/frontier-redis-failure-semantics.md). In particular,
    a caller deciding "no more work" must not treat this exception as
    equivalent to a legitimate empty/idle result.
    """


@dataclass(frozen=True)
class FrontierClaim:
    """Proof of exclusive ownership over one dequeued attempt at a URL.

    `token` is the sole proof of ownership: any renewal or completion call
    must present it, and a backend must reject one that no longer matches
    the URL's current claim (stale claim from a since-reclaimed/completed
    attempt).
    """

    url: str
    token: str
    attempt: int
    domain: str
    priority: int
    lease_expires_at: float
    source_query: str = ""


class Frontier(Protocol):
    """Contract implemented by every frontier backend.

    Behavioral guarantees are documented per-method in
    docs/architecture/frontier-adr.md §1 — the signatures alone are not
    the contract.

    Error contract: a backend that cannot complete an operation because of
    an infrastructure failure (not because of the frontier's actual state)
    must raise `FrontierUnavailable` rather than returning a value that
    could be mistaken for legitimate state (see
    docs/architecture/frontier-redis-failure-semantics.md). `URLFrontier`
    (pure in-memory) has no infrastructure to fail against and never raises
    it; `RedisURLFrontier` raises it from every method whose return value
    would otherwise be ambiguous with a real empty/false/zero result.
    """

    def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool: ...

    def get_next_url(self) -> FrontierClaim | None: ...

    def renew_claim(self, claim: FrontierClaim) -> FrontierClaim | None: ...

    def mark_visited(self, claim: FrontierClaim) -> None: ...

    def mark_failed(self, claim: FrontierClaim, error: str = "") -> None: ...

    def mark_deferred(self, claim: FrontierClaim, reason: str = "") -> None: ...

    def mark_skipped(self, claim: FrontierClaim) -> None: ...

    def has_pending(self) -> bool: ...

    def pending_count(self) -> int: ...

    def get_source_query(self, url: str) -> str: ...

    def get_status_counts(self) -> dict[str, int]: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...
