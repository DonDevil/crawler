"""Basic crawler integration test."""

import asyncio

import aiohttp
import pytest
from aiohttp import web

from core.url_frontier import URLFrontier
from crawler.async_crawler import AsyncCrawler
from parsers.html_link_extractor import HTMLLinkExtractor


async def _run_test_server():
    app = web.Application()

    async def handler_root(request):
        return web.Response(
            text="<html><body><a href='/page'>page</a></body></html>",
            content_type="text/html",
        )

    async def handler_page(request):
        return web.Response(
            text="<html><body><a href='/'>root</a></body></html>",
            content_type="text/html",
        )

    app.router.add_get("/", handler_root)
    app.router.add_get("/page", handler_page)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


async def _run_failing_server():
    app = web.Application()

    async def handler_fail(request):
        return web.Response(status=500, text="boom")

    app.router.add_get("/", handler_fail)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


@pytest.mark.asyncio
async def test_crawler_processes_a_few_pages():
    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier()
        frontier.add_url(base_url)

        crawler = AsyncCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=5,
            max_pages=3,
        )

        await crawler.run()

        assert base_url in frontier.visited
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_crawler_marks_failed_not_visited_on_persistent_http_error():
    runner, base_url = await _run_failing_server()

    try:
        frontier = URLFrontier(rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0)
        frontier.add_url(base_url)

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
        counts = frontier.get_status_counts()
        assert counts["inflight"] == 0
        assert counts["failed_permanent"] == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_worker_cancellation_mid_fetch_does_not_leave_claim_inflight():
    frontier = URLFrontier(rate_limit=0)
    frontier.add_url("https://example.com/slow")
    claim = frontier.get_next_url()
    assert claim is not None

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(3600)

    crawler = AsyncCrawler(frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1, timeout=5)
    crawler.fetch = hang_forever

    async with aiohttp.ClientSession() as session:
        worker_task = asyncio.create_task(crawler.worker(session))
        await crawler.queue.put(claim)
        await asyncio.sleep(0.05)  # let the worker dequeue the claim and start "fetching"

        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    counts = frontier.get_status_counts()
    assert counts["inflight"] == 0
    assert "https://example.com/slow" not in frontier.visited


@pytest.mark.asyncio
async def test_crawler_worker_exception_fails_claim_instead_of_leaving_it_inflight():
    runner, base_url = await _run_test_server()

    class ExplodingParser:
        def extract_content(self, html, url):
            raise RuntimeError("parser boom")

    try:
        frontier = URLFrontier(rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0)
        frontier.add_url(base_url)

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
        counts = frontier.get_status_counts()
        assert counts["inflight"] == 0
        assert counts["failed_permanent"] == 1
    finally:
        await runner.cleanup()
