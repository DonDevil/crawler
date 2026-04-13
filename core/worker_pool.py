"""Worker pool that runs crawler workers in parallel."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable, Optional

from loguru import logger


class WorkerPool:
    """A simple worker pool for processing tasks from an asyncio.Queue."""

    def __init__(
        self,
        queue: asyncio.Queue[str],
        worker_fn: Callable[[str], AsyncIterator[None]] | Callable[[str], None] | Callable[[str], asyncio.Future],
        concurrency: int = 10,
    ):
        self.queue = queue
        self.worker_fn = worker_fn
        self.concurrency = concurrency
        self._workers: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()

    async def _worker(self, worker_id: int):
        logger.debug(f"Worker {worker_id} started")

        while not self._stopped.is_set():
            try:
                url = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                result = self.worker_fn(url)

                if asyncio.iscoroutine(result):
                    await result

            except Exception as exc:
                logger.exception(f"Worker {worker_id} failed on {url}: {exc}")

            finally:
                self.queue.task_done()

        logger.debug(f"Worker {worker_id} stopped")

    async def start(self):
        """Start all worker tasks."""
        self._stopped.clear()
        self._workers = [
            asyncio.create_task(self._worker(i + 1)) for i in range(self.concurrency)
        ]

    async def stop(self):
        """Stop all workers gracefully."""
        self._stopped.set()

        for task in self._workers:
            task.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)

    async def join(self):
        """Wait until the queue is fully processed."""
        await self.queue.join()
