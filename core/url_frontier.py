"""Priority URL frontier with per-domain politeness controls."""

from __future__ import annotations

import heapq
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from loguru import logger

from storage.url_database import URLDatabase
from utils.url_utils import URLUtils


class URLFrontier:
    """Manage pending crawl URLs with deduplication and host rate limiting."""

    def __init__(self, rate_limit: float = 1.0, url_database: URLDatabase | None = None):
        self.visited: set[str] = set()
        self._queued: set[str] = set()
        self._scheduled_domains: set[str] = set()
        self._sequence = 0
        self.domain_queues: dict[str, deque[tuple[int, int, str]]] = defaultdict(deque)
        self.domain_next_time: dict[str, float] = {}
        self.priority_queue: list[tuple[int, int, str]] = []
        self.rate_limit = rate_limit
        self.url_database = url_database

    def add_url(self, url: str, priority: int = 10) -> None:
        """Add a URL to the frontier if it has not already been seen."""

        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            return

        if cleaned in self.visited or cleaned in self._queued:
            return

        domain = urlparse(cleaned).netloc
        self._sequence += 1
        self.domain_queues[domain].append((priority, self._sequence, cleaned))
        self._queued.add(cleaned)
        self._schedule_domain(domain)

        if self.url_database is not None:
            self.url_database.add_url(cleaned, status="queued")

        logger.debug(f"Added to frontier: {cleaned}")

    def _schedule_domain(self, domain: str) -> None:
        queue = self.domain_queues.get(domain)
        if not queue or domain in self._scheduled_domains:
            return

        priority, sequence, _ = queue[0]
        heapq.heappush(self.priority_queue, (priority, sequence, domain))
        self._scheduled_domains.add(domain)

    def get_next_url(self) -> str | None:
        """Return the next crawlable URL, respecting per-domain rate limits."""

        blocked_domains: list[str] = []
        now = time.time()

        while self.priority_queue:
            _, _, domain = heapq.heappop(self.priority_queue)
            self._scheduled_domains.discard(domain)

            queue = self.domain_queues.get(domain)
            if not queue:
                continue

            next_time = self.domain_next_time.get(domain, 0)
            if now < next_time:
                blocked_domains.append(domain)
                continue

            _, _, url = queue.popleft()
            self.domain_next_time[domain] = now + self.rate_limit

            if queue:
                self._schedule_domain(domain)
            else:
                self.domain_queues.pop(domain, None)

            for blocked_domain in blocked_domains:
                self._schedule_domain(blocked_domain)

            return url

        for blocked_domain in blocked_domains:
            self._schedule_domain(blocked_domain)

        return None

    def mark_visited(self, url: str) -> None:
        """Mark a URL as visited so it is not crawled again."""

        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            return

        self.visited.add(cleaned)
        self._queued.discard(cleaned)

    def has_pending(self) -> bool:
        """Return True when the frontier still has queued work."""

        return bool(self.priority_queue or self.domain_queues)

    def pending_count(self) -> int:
        """Return the number of queued URLs that have not been visited yet."""

        return sum(len(queue) for queue in self.domain_queues.values())