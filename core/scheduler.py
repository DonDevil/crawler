"""Scheduler for the crawler.

The scheduler pulls URLs from the frontier and pushes them into the worker queue.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from core.url_frontier import URLFrontier


class Scheduler:

    def __init__(
        self,
        frontier: URLFrontier,
        queue: asyncio.Queue[str],
        poll_interval: float = 0.5,
    ):
        self.frontier = frontier
        self.queue = queue
        self.poll_interval = poll_interval
        self._stopped = asyncio.Event()

    async def run(self):
        """Continuously schedule URLs from the frontier into the work queue."""
        logger.debug("Scheduler started")

        while not self._stopped.is_set():
            url = self.frontier.get_next_url()

            if url:
                await self.queue.put(url)
                logger.debug(f"Scheduled URL: {url}")
                continue

            # nothing available; wait for new URLs to appear
            await asyncio.sleep(self.poll_interval)

        logger.debug("Scheduler stopped")

    def stop(self):
        """Signal the scheduler to stop."""
        self._stopped.set()
