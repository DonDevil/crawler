"""Tests for non-async crawler engines."""

import os

import pytest
from aiohttp import web

from core.url_frontier import URLFrontier
from crawler.http_crawler import HTTPCrawler
from crawler.playwright_crawler import PlaywrightCrawler
from crawler.selenium_crawler import SeleniumCrawler
from crawler.tor_crawler import TorCrawler
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


@pytest.mark.asyncio
async def test_http_crawler_processes_pages():
    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier()
        frontier.add_url(base_url)

        crawler = HTTPCrawler(
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
async def test_tor_crawler_handles_clearweb_with_direct_fallback():
    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier()
        frontier.add_url(base_url)

        crawler = TorCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=2,
            max_pages=2,
            timeout=5,
            max_retries=1,
            use_tor_for_clearweb=False,
        )

        await crawler.run()

        assert base_url in frontier.visited
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_BROWSER_CRAWLER_TESTS") != "1",
    reason="Set RUN_BROWSER_CRAWLER_TESTS=1 to run browser-based crawler tests",
)
async def test_playwright_crawler_processes_page_when_available():
    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier()
        frontier.add_url(base_url)

        crawler = PlaywrightCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=1,
            max_pages=1,
            timeout=10,
            max_retries=1,
        )

        await crawler.run()
        assert base_url in frontier.visited
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_BROWSER_CRAWLER_TESTS") != "1",
    reason="Set RUN_BROWSER_CRAWLER_TESTS=1 to run browser-based crawler tests",
)
async def test_selenium_crawler_processes_page_when_available():
    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier()
        frontier.add_url(base_url)

        crawler = SeleniumCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=1,
            max_pages=1,
            timeout=10,
            max_retries=1,
        )

        await crawler.run()
        assert base_url in frontier.visited
    finally:
        await runner.cleanup()