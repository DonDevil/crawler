"""Basic crawler integration test."""

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
