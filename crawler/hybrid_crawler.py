"""Hybrid crawler that routes each URL to the best crawler engine."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Optional

import aiohttp
import httpx
from aiohttp_socks import ProxyConnector
from loguru import logger

from core.crawler_router import CrawlerRouter
from crawler.async_crawler import AsyncCrawler
from crawler.http_crawler import HTTPCrawler
from crawler.playwright_crawler import PlaywrightCrawler
from crawler.selenium_crawler import SeleniumCrawler
from crawler.tor_crawler import TorCrawler
from storage.url_database import URLDatabase
from tor.proxy_config import get_default_tor_proxy
from utils.request_headers import get_default_headers


class HybridCrawler:
    """Use one shared frontier and route each URL to the best fetch strategy."""

    def __init__(
        self,
        frontier,
        parser=None,
        concurrency=25,
        timeout=15,
        max_retries=3,
        max_pages: Optional[int] = None,
        user_agent: Optional[str] = None,
        url_database: Optional[URLDatabase] = None,
    ):
        self.frontier = frontier
        self.parser = parser
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.user_agent = user_agent
        self.url_database = url_database
        self.router = CrawlerRouter()

        self.queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._pages_crawled = 0
        self._pages_failed = 0
        self._engine_counts: Counter[str] = Counter()

        self._direct_session: aiohttp.ClientSession | None = None
        self._tor_session: aiohttp.ClientSession | None = None
        self._httpx_client: httpx.AsyncClient | None = None
        self._httpx_tor_client: httpx.AsyncClient | None = None

        self._playwright_ready = False
        self._playwright_lock = asyncio.Lock()

        self._http_semaphore = asyncio.Semaphore(max(1, min(10, concurrency)))
        self._tor_semaphore = asyncio.Semaphore(max(1, min(5, concurrency)))
        self._playwright_semaphore = asyncio.Semaphore(2)
        self._selenium_semaphore = asyncio.Semaphore(1)

        common_args = {
            "frontier": self.frontier,
            "parser": self.parser,
            "concurrency": self.concurrency,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "max_pages": self.max_pages,
            "user_agent": self.user_agent,
            "url_database": self.url_database,
        }
        self._async_engine = AsyncCrawler(**common_args)
        self._http_engine = HTTPCrawler(**common_args)
        self._tor_engine = TorCrawler(**common_args)
        self._playwright_engine = PlaywrightCrawler(**common_args)
        self._selenium_engine = SeleniumCrawler(**common_args)

    async def _ensure_playwright_ready(self) -> tuple[bool, Optional[str]]:
        if self._playwright_ready:
            return True, None

        async with self._playwright_lock:
            if self._playwright_ready:
                return True, None

            try:
                await self._playwright_engine._start_browser()
            except Exception as exc:
                return False, str(exc)

            self._playwright_ready = True
            return True, None

    async def _fetch_with_engine(self, engine_name: str, url: str) -> tuple[Optional[str], Optional[str]]:
        if engine_name == "async":
            if self._direct_session is None:
                return None, "Direct session unavailable"
            return await self._async_engine.fetch(self._direct_session, url, tor_session=self._tor_session)

        if engine_name == "http":
            if self._httpx_client is None:
                return None, "HTTP client unavailable"
            async with self._http_semaphore:
                return await self._http_engine.fetch(self._httpx_client, url)

        if engine_name == "tor":
            if self._httpx_client is None or self._httpx_tor_client is None:
                return None, "Tor clients unavailable"
            async with self._tor_semaphore:
                return await self._tor_engine.fetch(url, self._httpx_tor_client, self._httpx_client)

        if engine_name == "playwright":
            ready, error = await self._ensure_playwright_ready()
            if not ready:
                return None, error or "Playwright unavailable"
            async with self._playwright_semaphore:
                return await self._playwright_engine.fetch(url)

        if engine_name == "selenium":
            async with self._selenium_semaphore:
                return await self._selenium_engine.fetch(url)

        return None, f"Unsupported engine: {engine_name}"

    @staticmethod
    def _prepend_unique(plan: list[str], new_engines: list[str], attempted: set[str]) -> list[str]:
        merged: list[str] = []
        seen = set(attempted)

        for engine in [*new_engines, *plan]:
            if engine in seen:
                continue
            seen.add(engine)
            merged.append(engine)

        return merged

    async def worker(self):
        while not self._stop_event.is_set():
            url = await self.queue.get()

            try:
                if not url:
                    continue

                if self.url_database:
                    self.url_database.add_url(url, status="pending")

                plan = list(self.router.get_engine_plan(url))
                attempted: set[str] = set()
                html: Optional[str] = None
                failure_reason: Optional[str] = None
                engine_used = "unknown"

                while plan:
                    engine_used = plan.pop(0)
                    if engine_used in attempted:
                        continue

                    attempted.add(engine_used)
                    html, failure_reason = await self._fetch_with_engine(engine_used, url)

                    if html:
                        if engine_used in {"async", "http"} and self.router.needs_browser_upgrade(url, html=html):
                            failure_reason = "Content requires browser rendering"
                            html = None
                            plan = self._prepend_unique(
                                plan,
                                self.router.get_engine_plan(
                                    url,
                                    current_engine=engine_used,
                                    failure_reason=failure_reason,
                                ),
                                attempted,
                            )
                            continue
                        break

                    plan = self._prepend_unique(
                        plan,
                        self.router.get_engine_plan(
                            url,
                            current_engine=engine_used,
                            failure_reason=failure_reason,
                        ),
                        attempted,
                    )

                status = "visited"
                if html and self.parser:
                    links = self.parser.extract_links(html, url)
                    for link in links:
                        self.frontier.add_url(link)
                elif failure_reason:
                    status = "failed"
                    self._pages_failed += 1
                    logger.warning(f"Failed to crawl {url}: {failure_reason}")

                self.frontier.mark_visited(url)
                if self.url_database:
                    self.url_database.update_status(url, status)

                self._pages_crawled += 1
                self._engine_counts[engine_used] += 1
                logger.info(f"Processed ({self._pages_crawled}): {url} [{status}] via {engine_used}")

                if self.max_pages and self._pages_crawled >= self.max_pages:
                    logger.info("Reached max pages limit, stopping crawler")
                    self._stop_event.set()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Worker error for {url}: {exc}")
            finally:
                self.queue.task_done()

    async def scheduler(self):
        idle_loops = 0

        while not self._stop_event.is_set():
            url = self.frontier.get_next_url()

            if url:
                idle_loops = 0
                await self.queue.put(url)
                continue

            if self.queue.empty():
                idle_loops += 1
                if idle_loops >= 10:
                    logger.info("No more URLs to crawl, stopping crawler")
                    self._stop_event.set()
                    break
            else:
                idle_loops = 0

            await asyncio.sleep(0.5)

    async def run(self):
        connector = aiohttp.TCPConnector(limit=self.concurrency)
        tor_proxy = get_default_tor_proxy()
        tor_connector = ProxyConnector.from_url(tor_proxy.replace("socks5h://", "socks5://", 1), limit=max(1, min(10, self.concurrency)))
        headers = get_default_headers(self.user_agent)

        async with aiohttp.ClientSession(connector=connector) as direct_session, aiohttp.ClientSession(connector=tor_connector) as tor_session, httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
            limits=httpx.Limits(max_connections=self.concurrency),
        ) as httpx_client, httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
            proxy=tor_proxy,
            limits=httpx.Limits(max_connections=max(1, min(10, self.concurrency))),
        ) as httpx_tor_client:
            self._direct_session = direct_session
            self._tor_session = tor_session
            self._httpx_client = httpx_client
            self._httpx_tor_client = httpx_tor_client

            workers = [
                asyncio.create_task(self.worker())
                for _ in range(self.concurrency)
            ]
            scheduler_task = asyncio.create_task(self.scheduler())

            try:
                await self._stop_event.wait()
            finally:
                scheduler_task.cancel()
                for task in workers:
                    task.cancel()

                await asyncio.gather(scheduler_task, *workers, return_exceptions=True)

                if self._playwright_ready:
                    await self._playwright_engine._stop_browser()

                logger.info(
                    "Hybrid crawler finished: processed={} failed={} pending_frontier={} engine_usage={}",
                    self._pages_crawled,
                    self._pages_failed,
                    self.frontier.pending_count(),
                    dict(self._engine_counts),
                )
