"""Crawler-backend-level integration tests for claim heartbeat wiring
(docs/architecture/frontier-adr.md §8, migration Step 5).

core/claim_heartbeat.py's own mechanics (renewal cadence, stale-claim
handling, cancellation, thread routing) are covered directly in
tests/claim_heartbeat_test.py. This file proves the wiring inside
crawler/async_crawler.py's `worker()` behaves correctly end to end: the
heartbeat actually starts once a claim exists, stops on every exit path
(success/failure/skip/exception/cancellation), and a lost claim is never
treated as successfully owned.
"""

import asyncio

import aiohttp
import pytest
from aiohttp import web

from core.frontier import FrontierClaim
from core.url_frontier import URLFrontier
from crawler.async_crawler import AsyncCrawler
from parsers.html_link_extractor import HTMLLinkExtractor


async def _run_delayed_server(delay: float, status: int = 200):
    app = web.Application()

    async def handler(request):
        await asyncio.sleep(delay)
        if status == 200:
            return web.Response(text="<html><body>ok</body></html>", content_type="text/html")
        return web.Response(status=status, text="boom")

    app.router.add_get("/", handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


def _spy_on_renew(frontier: URLFrontier) -> list:
    calls: list = []
    original = frontier.renew_claim

    def spy(claim):
        calls.append(claim)
        return original(claim)

    frontier.renew_claim = spy
    return calls


@pytest.mark.asyncio
async def test_successful_completion_stops_heartbeat():
    runner, base_url = await _run_delayed_server(delay=0.25)
    try:
        frontier = URLFrontier(rate_limit=0, lease_ttl=0.15)
        frontier.add_url(base_url)
        calls = _spy_on_renew(frontier)

        crawler = AsyncCrawler(
            frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1, max_pages=1, timeout=5
        )
        assert crawler.heartbeat_interval < 0.15

        await crawler.run()

        assert base_url in frontier.visited
        assert len(calls) >= 1, "heartbeat never fired during the slow fetch"

        calls_at_finish = len(calls)
        await asyncio.sleep(0.3)
        assert len(calls) == calls_at_finish, "heartbeat kept renewing after the worker finished"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_failed_completion_stops_heartbeat():
    runner, base_url = await _run_delayed_server(delay=0.15, status=500)
    try:
        frontier = URLFrontier(rate_limit=0, lease_ttl=0.1, max_retries=1, base_backoff=0, max_backoff=0)
        frontier.add_url(base_url)
        calls = _spy_on_renew(frontier)

        crawler = AsyncCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=1,
            max_pages=1,
            timeout=5,
            max_retries=1,
        )

        await crawler.run()

        assert base_url not in frontier.visited
        assert frontier.get_status_counts()["failed_permanent"] == 1
        assert frontier.get_status_counts()["inflight"] == 0

        calls_at_finish = len(calls)
        await asyncio.sleep(0.3)
        assert len(calls) == calls_at_finish, "heartbeat kept renewing after the worker failed the claim"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_skipped_completion_never_starts_heartbeat():
    from utils.url_utils import URLUtils

    frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
    calls = _spy_on_renew(frontier)

    crawler = AsyncCrawler(frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1, max_pages=1, timeout=5)

    blacklisted_url = "https://example.com/blacklisted"
    claim = FrontierClaim(
        url=blacklisted_url, token="tok", attempt=1, domain="example.com", priority=10, lease_expires_at=0.0
    )
    frontier._active_claims[blacklisted_url] = "tok"

    was_blacklisted = URLUtils.is_blacklisted
    URLUtils.is_blacklisted = staticmethod(lambda url: True)
    try:
        async with aiohttp.ClientSession() as session:
            worker_task = asyncio.create_task(crawler.worker(session))
            await crawler.queue.put(claim)
            await asyncio.wait_for(crawler.queue.join(), timeout=2.0)
            crawler._stop_event.set()
            worker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker_task
    finally:
        URLUtils.is_blacklisted = was_blacklisted

    assert not calls, "heartbeat must never start for a claim that's immediately skipped"
    assert blacklisted_url in frontier._skipped


@pytest.mark.asyncio
async def test_worker_exception_stops_heartbeat():
    runner, base_url = await _run_delayed_server(delay=0.15)

    class ExplodingParser:
        def extract_content(self, html, url):
            raise RuntimeError("parser boom")

    try:
        frontier = URLFrontier(rate_limit=0, lease_ttl=0.1, max_retries=1, base_backoff=0, max_backoff=0)
        frontier.add_url(base_url)
        calls = _spy_on_renew(frontier)

        crawler = AsyncCrawler(
            frontier=frontier,
            parser=ExplodingParser(),
            concurrency=1,
            max_pages=1,
            timeout=5,
            max_retries=1,
        )

        await crawler.run()

        assert base_url not in frontier.visited
        assert frontier.get_status_counts()["failed_permanent"] == 1
        assert frontier.get_status_counts()["inflight"] == 0

        calls_at_finish = len(calls)
        await asyncio.sleep(0.3)
        assert len(calls) == calls_at_finish, "heartbeat kept renewing after the worker raised"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_worker_cancellation_stops_heartbeat():
    frontier = URLFrontier(rate_limit=0, lease_ttl=0.1)
    frontier.add_url("https://example.com/slow-cancel")
    claim = frontier.get_next_url()
    assert claim is not None
    calls = _spy_on_renew(frontier)

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(3600)

    crawler = AsyncCrawler(frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1, timeout=5)
    crawler.fetch = hang_forever

    async with aiohttp.ClientSession() as session:
        worker_task = asyncio.create_task(crawler.worker(session))
        await crawler.queue.put(claim)
        await asyncio.sleep(0.25)  # let a couple of heartbeat ticks fire

        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    assert len(calls) >= 1, "expected at least one heartbeat tick before cancellation"
    counts = frontier.get_status_counts()
    assert counts["inflight"] == 0
    assert "https://example.com/slow-cancel" not in frontier.visited

    calls_at_finish = len(calls)
    await asyncio.sleep(0.3)
    assert len(calls) == calls_at_finish, "heartbeat kept renewing after the worker was cancelled"


@pytest.mark.asyncio
async def test_claim_lost_mid_fetch_is_not_marked_completed():
    """If renew_claim reports the claim as stale/lost mid-fetch, the worker
    must not call mark_visited/mark_failed/mark_skipped with it -- it is no
    longer this worker's claim to resolve (ADR §8 / requirement 12)."""
    frontier = URLFrontier(rate_limit=0, lease_ttl=0.05)
    frontier.add_url("https://example.com/reclaimed-mid-fetch")
    claim = frontier.get_next_url()
    assert claim is not None

    # Simulate another worker/recovery sweep completing (and thus
    # invalidating) this claim while our worker is still "fetching".
    frontier.mark_visited(claim)
    frontier.visited.discard(claim.url)  # isolate: only prove *this* worker didn't also complete it

    mark_visited_calls = []
    mark_failed_calls = []
    original_mark_visited = frontier.mark_visited
    original_mark_failed = frontier.mark_failed
    frontier.mark_visited = lambda c: (mark_visited_calls.append(c), original_mark_visited(c))[-1]
    frontier.mark_failed = lambda c, e="": (mark_failed_calls.append(c), original_mark_failed(c, e))[-1]

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "<html></html>", None

    crawler = AsyncCrawler(frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1, timeout=5)
    crawler.fetch = slow_fetch

    async with aiohttp.ClientSession() as session:
        worker_task = asyncio.create_task(crawler.worker(session))
        await crawler.queue.put(claim)
        await asyncio.sleep(0.3)  # long enough for the heartbeat to discover the claim is gone

        crawler._stop_event.set()
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    assert not mark_visited_calls, "worker must not mark a lost claim as visited"
    assert not mark_failed_calls, "worker must not mark a lost claim as failed either"
