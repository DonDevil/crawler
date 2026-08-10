"""Regression tests for Phase 1 of the Redis/SQLite boundary cleanup
(docs/architecture/redis-sqlite-boundary-decision.md,
docs/architecture/sql-persistence-audit.md).

Redis mode must no longer write URL frontier state to `URLDatabase` during
normal crawl execution -- `RedisURLFrontier` no longer accepts a
`url_database` argument at all, and the crawler engines only mirror status
into `url_database` when the active frontier is the local, in-process
`URLFrontier` (`AsyncCrawler._sql_mode_mirror` and its equivalent in the
other 6 engines).

These tests lock in the two concrete defects the removed mirror caused:

1. Retry-status clobber: a URL Redis correctly holds as `retry_scheduled`
   must never be reported by `url_database` as terminally `failed` --
   verified here by proving `url_database` isn't touched at all for a
   Redis-mode crawl, which is a stronger guarantee than "the status is
   merely correct."
2. SQLite-failure-can't-interrupt-discovery: a URLDatabase fault while
   processing a page's discovered links must not abort the remaining links
   or turn a Redis-side success into a reported page failure -- verified
   against the actual new boundary (url_database is never called on this
   path in Redis mode), not a simulated architecture.

Run this test ONLY if Redis is available locally on port 6379 (matches the
skip convention in tests/redis_frontier_test.py).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid

import pytest
import redis
from aiohttp import web

from core.redis_frontier import RedisURLFrontier
from crawler.async_crawler import AsyncCrawler
from parsers.html_link_extractor import HTMLLinkExtractor
from storage.url_database import URLDatabase


@pytest.fixture
def redis_frontier():
    """A real Redis frontier on a throwaway namespace, matching
    tests/redis_frontier_test.py's convention (db=1, skip if unavailable)."""
    try:
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace=f"test_mirror_removal_{uuid.uuid4().hex[:8]}",
            rate_limit=0,
        )
        frontier.clear()
        yield frontier
        frontier.clear()
        frontier.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


@pytest.fixture
def url_database():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    db = URLDatabase(path=path)
    yield db
    db.close()

    try:
        os.remove(path)
    except OSError:
        pass


class FailingURLDatabase(URLDatabase):
    """Simulates the disk-fault class of failure the audit calls out
    (`sqlite3.OperationalError`: disk full, busy_timeout exceeded, WAL
    corruption) on every write. Reads for call-counting still work."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_calls = 0

    def add_url(self, url: str, status: str = "pending") -> None:
        self.write_calls += 1
        raise sqlite3.OperationalError("simulated disk fault")

    def update_status(self, url: str, status: str) -> None:
        self.write_calls += 1
        raise sqlite3.OperationalError("simulated disk fault")


async def _run_failing_server():
    """A server that always returns HTTP 500 -- triggers the crawler's
    failure path without exhausting frontier-level retries in one claim."""
    app = web.Application()

    async def handler(request):
        return web.Response(status=500, text="boom")

    app.router.add_get("/", handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


async def _run_link_page_server(n_links: int):
    """A server whose root page links to `n_links` distinct same-domain
    paths -- exercises the discovered-links loop without needing to serve
    those follow-on paths (the test only checks they were *added* to the
    frontier, not fully crawled)."""
    app = web.Application()

    async def handler_root(request):
        links = "".join(f'<a href="/leaf{i}">leaf{i}</a>' for i in range(n_links))
        return web.Response(text=f"<html><body>{links}</body></html>", content_type="text/html")

    app.router.add_get("/", handler_root)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


@pytest.mark.asyncio
async def test_retry_scheduled_url_is_not_written_to_sqlite_as_failed(redis_frontier, url_database):
    """Before this change, RedisURLFrontier._complete() correctly wrote
    db_status="queued" for a retry-scheduled outcome, but the worker's own
    duplicate terminal write immediately overwrote it back to "failed"
    (core/redis_frontier.py vs. crawler/async_crawler.py, pre-fix). After
    the fix, url_database is not part of the Redis crawl-time hot path at
    all, so this URL must be completely absent from it -- a stronger
    guarantee than "the status happens to be right."
    """
    runner, base_url = await _run_failing_server()

    try:
        # max_retries=3 on the frontier so the first failed attempt lands in
        # retry_scheduled, not failed_permanent (attempt 1 < max_retries).
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace=f"test_retry_clobber_{uuid.uuid4().hex[:8]}",
            rate_limit=0,
            max_retries=3,
            base_backoff=1000,  # large: stays in retry_scheduled for the assertion window
            max_backoff=1000,
        )
        frontier.clear()
        try:
            frontier.add_url(base_url, priority=10)

            crawler = AsyncCrawler(
                frontier=frontier,
                parser=HTMLLinkExtractor(),
                concurrency=1,
                max_pages=1,
                timeout=5,
                max_retries=1,  # fail fast inside fetch(), no per-fetch retry loop
                url_database=url_database,
            )

            await crawler.run()

            counts = frontier.get_status_counts()
            assert counts["retry_scheduled"] == 1, (
                f"Redis should hold the URL as retry_scheduled, got {counts}"
            )
            assert counts["failed_permanent"] == 0

            # The real assertion: url_database was never touched for this
            # Redis-mode crawl, so no clobbered "failed" row can exist.
            assert url_database.get_all_urls() == [], (
                "Redis-mode crawl must not write anything to url_database"
            )
        finally:
            frontier.clear()
            frontier.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_url_database_failure_cannot_interrupt_redis_discovery(redis_frontier):
    """Before this change, RedisURLFrontier.add_url()'s SQLite mirror write
    ran inline in the discovered-links loop with no try/except -- a SQLite
    fault on link N aborted links N+1..end and reported an otherwise-
    successful Redis-side page fetch as failed
    (docs/architecture/sql-persistence-audit.md §8).

    After the fix, url_database is never called during Redis-mode
    discovery at all, so even a URLDatabase that raises on every write must
    not affect the outcome: every discovered link must land in Redis and
    the page must be marked visited.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    failing_db = FailingURLDatabase(path=path)

    runner, base_url = await _run_link_page_server(n_links=5)

    try:
        crawler = AsyncCrawler(
            frontier=redis_frontier,
            parser=HTMLLinkExtractor(),
            concurrency=1,
            max_pages=1,
            timeout=5,
            url_database=failing_db,
        )

        redis_frontier.add_url(base_url, priority=10)

        await crawler.run()

        counts = redis_frontier.get_status_counts()
        assert counts["visited"] == 1, f"Root page should be marked visited, got {counts}"
        assert counts["queued"] == 5, (
            f"All 5 discovered links should have reached Redis despite the "
            f"failing URLDatabase, got {counts}"
        )
        assert crawler._pages_failed == 0, (
            "A URLDatabase fault must not turn a successful Redis-side page "
            "fetch into a reported failure"
        )

        # Proves the new boundary directly: url_database's failing write
        # methods were never even invoked in Redis mode.
        assert failing_db.write_calls == 0, (
            "Redis-mode crawl must never call url_database write methods"
        )
    finally:
        await runner.cleanup()
        failing_db.close()
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
