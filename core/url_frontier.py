"""Priority URL frontier with per-domain politeness controls."""

from __future__ import annotations

import heapq
import time
import uuid
from collections import defaultdict, deque
from dataclasses import replace
from urllib.parse import urlparse

from loguru import logger

from core.frontier import FrontierClaim
from storage.url_database import URLDatabase
from utils.url_utils import URLUtils


class URLFrontier:
    """Manage pending crawl URLs with deduplication and host rate limiting.

    Implements the `Frontier` protocol (core/frontier.py). Claim completion
    is always synchronous within this process, so there is no crash-recovery
    scenario to build for here: `_active_claims`/`_attempts` exist for token
    validation and retry bookkeeping, not lease expiry.
    """

    def __init__(
        self,
        rate_limit: float = 1.0,
        url_database: URLDatabase | None = None,
        max_retries: int = 3,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        lease_ttl: float = 90.0,
    ):
        self.visited: set[str] = set()
        self._known: set[str] = set()
        self._skipped: set[str] = set()
        self._failed_permanent: set[str] = set()
        self._scheduled_domains: set[str] = set()
        self._sequence = 0
        self.domain_queues: dict[str, deque[tuple[int, int, str]]] = defaultdict(deque)
        self.domain_next_time: dict[str, float] = {}
        self.priority_queue: list[tuple[int, int, str]] = []
        self.rate_limit = rate_limit
        self.url_database = url_database
        self._url_to_query: dict[str, str] = {}

        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.lease_ttl = lease_ttl
        self._attempts: dict[str, int] = {}
        self._active_claims: dict[str, str] = {}
        self._retry_heap: list[tuple[float, int, int, str]] = []

    def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool:
        """Add a URL to the frontier if it has not already been seen.

        Args:
            url: URL to add
            priority: Priority for crawling (lower = sooner)
            source_query: Optional query that discovered this URL
        """

        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            if URLUtils.is_blacklisted(url):
                logger.debug(f"Ignoring blacklisted URL before queueing: {url}")
            return False

        if cleaned in self._known:
            return False

        if self.url_database is not None and self.url_database.is_visited(cleaned):
            self.visited.add(cleaned)
            self._known.add(cleaned)
            logger.debug(f"Skipping already-visited URL from storage: {cleaned}")
            return False

        domain = urlparse(cleaned).netloc
        self._sequence += 1
        self.domain_queues[domain].append((priority, self._sequence, cleaned))
        self._known.add(cleaned)
        if source_query:
            self._url_to_query[cleaned] = source_query
        self._schedule_domain(domain)

        if self.url_database is not None:
            self.url_database.add_url(cleaned, status="queued")

        logger.debug(f"Added to frontier: {cleaned}")
        return True

    def _schedule_domain(self, domain: str) -> None:
        queue = self.domain_queues.get(domain)
        if not queue or domain in self._scheduled_domains:
            return

        priority, sequence, _ = queue[0]
        heapq.heappush(self.priority_queue, (priority, sequence, domain))
        self._scheduled_domains.add(domain)

    def _promote_due_retries(self, now: float) -> None:
        """Move retry-scheduled URLs whose backoff has expired back into their domain queues."""

        while self._retry_heap and self._retry_heap[0][0] <= now:
            _, priority, _, url = heapq.heappop(self._retry_heap)
            domain = urlparse(url).netloc
            self._sequence += 1
            self.domain_queues[domain].append((priority, self._sequence, url))
            self._schedule_domain(domain)

    def _issue_claim(self, url: str, domain: str, priority: int, now: float) -> FrontierClaim:
        attempt = self._attempts.get(url, 0) + 1
        self._attempts[url] = attempt
        token = uuid.uuid4().hex
        self._active_claims[url] = token

        return FrontierClaim(
            url=url,
            token=token,
            attempt=attempt,
            domain=domain,
            priority=priority,
            lease_expires_at=now + self.lease_ttl,
            source_query=self._url_to_query.get(url, ""),
        )

    def _requeue_or_drop_domain(self, domain: str, queue: deque[tuple[int, int, str]]) -> None:
        if queue:
            self._schedule_domain(domain)
        else:
            self.domain_queues.pop(domain, None)

    def _skip_blacklisted(self, url: str, domain: str, queue: deque[tuple[int, int, str]]) -> None:
        self._skipped.add(url)
        self._attempts.pop(url, None)
        if self.url_database is not None:
            self.url_database.update_status(url, "skipped")
        logger.info(f"Skipping blacklisted URL from frontier: {url}")
        self._requeue_or_drop_domain(domain, queue)

    def get_next_url(self) -> FrontierClaim | None:
        """Claim the next crawlable URL, respecting per-domain rate limits."""

        now = time.time()
        self._promote_due_retries(now)

        blocked_domains: list[str] = []

        while self.priority_queue:
            _, _, domain = heapq.heappop(self.priority_queue)
            self._scheduled_domains.discard(domain)

            queue = self.domain_queues.get(domain)
            if not queue:
                continue

            if now < self.domain_next_time.get(domain, 0):
                blocked_domains.append(domain)
                continue

            priority, _, url = queue.popleft()

            if URLUtils.is_blacklisted(url):
                self._skip_blacklisted(url, domain, queue)
                continue

            self.domain_next_time[domain] = now + self.rate_limit
            self._requeue_or_drop_domain(domain, queue)

            for blocked_domain in blocked_domains:
                self._schedule_domain(blocked_domain)

            return self._issue_claim(url, domain, priority, now)

        for blocked_domain in blocked_domains:
            self._schedule_domain(blocked_domain)

        return None

    def _is_current_claim(self, claim: FrontierClaim) -> bool:
        return self._active_claims.get(claim.url) == claim.token

    def renew_claim(self, claim: FrontierClaim) -> FrontierClaim | None:
        """Refresh a claim's lease iff its token is still the current owner."""

        if not self._is_current_claim(claim):
            return None

        return replace(claim, lease_expires_at=time.time() + self.lease_ttl)

    def mark_visited(self, claim: FrontierClaim) -> None:
        """Mark a claimed URL as terminally visited, iff the claim is still current."""

        if not self._is_current_claim(claim):
            logger.debug(f"Stale claim ignored (mark_visited): {claim.url}")
            return

        self._active_claims.pop(claim.url, None)
        self._attempts.pop(claim.url, None)
        self.visited.add(claim.url)

    def mark_skipped(self, claim: FrontierClaim) -> None:
        """Mark a claimed URL as terminally skipped (never retried), iff the claim is current."""

        if not self._is_current_claim(claim):
            logger.debug(f"Stale claim ignored (mark_skipped): {claim.url}")
            return

        self._active_claims.pop(claim.url, None)
        self._attempts.pop(claim.url, None)
        self._skipped.add(claim.url)

    def mark_failed(self, claim: FrontierClaim, error: str = "") -> None:
        """Record a failed attempt, retrying with backoff until `max_retries` is exhausted."""

        if not self._is_current_claim(claim):
            logger.debug(f"Stale claim ignored (mark_failed): {claim.url}")
            return

        self._active_claims.pop(claim.url, None)

        if claim.attempt < self.max_retries:
            backoff = min(self.base_backoff * (2 ** (claim.attempt - 1)), self.max_backoff)
            self._sequence += 1
            heapq.heappush(
                self._retry_heap,
                (time.time() + backoff, claim.priority, self._sequence, claim.url),
            )
            logger.debug(
                f"Retry scheduled for {claim.url} in {backoff:.1f}s "
                f"(attempt {claim.attempt}/{self.max_retries}): {error}"
            )
        else:
            self._attempts.pop(claim.url, None)
            self._failed_permanent.add(claim.url)
            logger.info(f"{claim.url} failed permanently after {claim.attempt} attempts: {error}")

    def has_pending(self) -> bool:
        """Return True when the frontier still has queued, in-flight, or retry-pending work."""

        return bool(
            self.priority_queue
            or self.domain_queues
            or self._active_claims
            or self._retry_heap
        )

    def pending_count(self) -> int:
        """Return the number of queued + retry-pending URLs (in-flight excluded)."""

        return sum(len(queue) for queue in self.domain_queues.values()) + len(self._retry_heap)

    def get_source_query(self, url: str) -> str:
        """Return the source query for a URL, or empty string if not found."""

        cleaned = URLUtils.normalize_url(url)
        return self._url_to_query.get(cleaned, "")

    def get_status_counts(self) -> dict[str, int]:
        """Return counts for every state in the frontier's URL state machine."""

        return {
            "queued": sum(len(queue) for queue in self.domain_queues.values()),
            "inflight": len(self._active_claims),
            "retry_scheduled": len(self._retry_heap),
            "visited": len(self.visited),
            "skipped": len(self._skipped),
            "failed_permanent": len(self._failed_permanent),
        }

    def clear(self) -> None:
        """Wipe all frontier state. Testing/reset only."""

        self.visited.clear()
        self._known.clear()
        self._skipped.clear()
        self._failed_permanent.clear()
        self._scheduled_domains.clear()
        self._sequence = 0
        self.domain_queues.clear()
        self.domain_next_time.clear()
        self.priority_queue.clear()
        self._url_to_query.clear()
        self._attempts.clear()
        self._active_claims.clear()
        self._retry_heap.clear()

    def close(self) -> None:
        """No owned resources to release; `url_database` is owned by the caller."""
