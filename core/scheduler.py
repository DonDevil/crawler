"""Scheduler for the crawler.

The scheduler pulls URLs from the frontier and pushes them into the worker queue.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from core.frontier import Frontier, FrontierClaim


class Scheduler:

    def __init__(
        self,
        frontier: Frontier,
        queue: asyncio.Queue[FrontierClaim],
        poll_interval: float = 0.5,
    ):
        self.frontier = frontier
        self.queue = queue
        self.poll_interval = poll_interval
        self._stopped = asyncio.Event()

    async def run(self):
        """Continuously schedule claimed URLs from the frontier into the work queue."""
        logger.debug("Scheduler started")

        while not self._stopped.is_set():
            claim = self.frontier.get_next_url()

            if claim:
                await self.queue.put(claim)
                logger.debug(f"Scheduled URL: {claim.url}")
                continue

            # nothing available; wait for new URLs to appear
            await asyncio.sleep(self.poll_interval)

        logger.debug("Scheduler stopped")

    def stop(self):
        """Signal the scheduler to stop."""
        self._stopped.set()
