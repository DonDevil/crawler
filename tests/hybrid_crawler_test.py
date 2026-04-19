"""Tests for hybrid crawler routing and execution."""

import pytest
from aiohttp import web

from core.url_frontier import URLFrontier
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


def test_router_selects_tor_for_onion_and_async_for_surface():
    from core.crawler_router import CrawlerRouter

    router = CrawlerRouter()

    assert router.select_crawler("http://somedomainhiddenservice.onion/") == "tor"
    assert router.select_crawler("https://example.com/") == "async"


@pytest.mark.asyncio
async def test_hybrid_crawler_processes_clearweb_pages():
    from crawler.hybrid_crawler import HybridCrawler

    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier(rate_limit=0)
        frontier.add_url(base_url)

        crawler = HybridCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=3,
            max_pages=2,
            timeout=5,
            max_retries=1,
        )

        await crawler.run()

        assert base_url in frontier.visited
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_hybrid_crawler_waits_for_inflight_fetches(monkeypatch):
    from crawler import hybrid_crawler as hybrid_module
    from crawler.hybrid_crawler import HybridCrawler

    original_sleep = hybrid_module.asyncio.sleep

    async def fast_idle_sleep(_delay):
        await original_sleep(0)

    async def slow_fetch(self, engine_name, url):
        await original_sleep(0.05)
        return "<html><body>ok</body></html>", None

    monkeypatch.setattr(hybrid_module.asyncio, "sleep", fast_idle_sleep)
    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", slow_fetch)

    frontier = URLFrontier(rate_limit=0)
    frontier.add_url("https://example.com/slow")

    crawler = HybridCrawler(
        frontier=frontier,
        parser=HTMLLinkExtractor(),
        concurrency=1,
        max_pages=1,
        timeout=5,
        max_retries=1,
    )

    await crawler.run()

    assert crawler._pages_crawled == 1
    assert "https://example.com/slow" in frontier.visited
