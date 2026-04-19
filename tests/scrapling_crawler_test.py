"""Tests for optional Scrapling anti-bot fallback."""

import pytest

from core.url_frontier import URLFrontier


@pytest.mark.asyncio
async def test_scrapling_crawler_fetches_result_content(monkeypatch):
    from crawler.scrapling_crawler import ScraplingCrawler

    class FakePage:
        status = 200
        html_content = "<html><body>ok</body></html>"

    monkeypatch.setattr(
        "crawler.scrapling_crawler.StealthyFetcher.fetch",
        lambda *args, **kwargs: FakePage(),
    )

    crawler = ScraplingCrawler(frontier=URLFrontier())
    html, error = await crawler.fetch("https://example.com")

    assert error is None
    assert "ok" in html


@pytest.mark.asyncio
async def test_scrapling_crawler_rejects_onion_urls():
    from crawler.scrapling_crawler import ScraplingCrawler

    crawler = ScraplingCrawler(frontier=URLFrontier())
    html, error = await crawler.fetch("http://hiddenserviceexample.onion")

    assert html is None
    assert "surface-web" in error.lower()
