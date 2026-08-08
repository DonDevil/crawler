"""Common frontier contract shared by every frontier backend (local, Redis, ...).

See docs/architecture/frontier-adr.md for the design this codifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
    """

    def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool: ...

    def get_next_url(self) -> FrontierClaim | None: ...

    def renew_claim(self, claim: FrontierClaim) -> FrontierClaim | None: ...

    def mark_visited(self, claim: FrontierClaim) -> None: ...

    def mark_failed(self, claim: FrontierClaim, error: str = "") -> None: ...

    def mark_skipped(self, claim: FrontierClaim) -> None: ...

    def has_pending(self) -> bool: ...

    def pending_count(self) -> int: ...

    def get_source_query(self, url: str) -> str: ...

    def get_status_counts(self) -> dict[str, int]: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...
