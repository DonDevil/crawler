import asyncio
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from loguru import logger

from storage.url_database import URLDatabase
from tor.proxy_config import get_default_tor_proxy
from utils.request_headers import get_default_headers
from utils.url_utils import URLUtils


class AsyncCrawler:

    def __init__(
        self,
        frontier,
        parser=None,
        concurrency=50,
        timeout=15,
        max_retries=3,
        max_pages: Optional[int] = None,
        user_agent: Optional[str] = None,
        url_database: Optional[URLDatabase] = None,
        tor_proxy: Optional[str] = None,
    ):

        self.frontier = frontier
        self.parser = parser
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.user_agent = user_agent
        self.url_database = url_database
        self.tor_proxy = tor_proxy or get_default_tor_proxy()

        self.queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._pages_crawled = 0
        self._pages_failed = 0

    async def fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        tor_session: Optional[aiohttp.ClientSession] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Fetch a URL and return a response body and optional failure reason.

        This method retries a few times and returns None on permanent failure.
        """

        headers = get_default_headers(self.user_agent)
        last_error: Optional[str] = None
        active_session = session

        if URLUtils.is_onion_url(url):
            if tor_session is None:
                return None, "Tor session unavailable for onion URL"
            active_session = tor_session

        for attempt in range(1, self.max_retries + 1):
            try:
                async with active_session.get(url, timeout=self.timeout, headers=headers) as response:
                    if response.status != 200:
                        last_error = f"HTTP {response.status}"
                        logger.warning(f"Fetch failed for {url}: {last_error}")

                        if response.status >= 500 and attempt < self.max_retries:
                            await asyncio.sleep(1)
                            continue

                        return None, last_error

                    content_type = response.headers.get("Content-Type", "")
                    if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
                        last_error = f"Unsupported content type: {content_type}"
                        logger.debug(f"Skipping non-HTML response for {url}: {content_type}")
                        return None, last_error

                    html = await response.text()
                    return html, None

            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Fetch failed ({attempt}/{self.max_retries}) {url}: {e}")
                await asyncio.sleep(1)

        logger.error(f"Giving up on {url}")
        return None, last_error or "unknown fetch error"

    async def worker(
        self,
        session: aiohttp.ClientSession,
        tor_session: Optional[aiohttp.ClientSession] = None,
    ):
        """Worker that consumes URLs from the queue and crawls them."""

        while not self._stop_event.is_set():
            url = await self.queue.get()

            try:
                if not url:
                    continue

                if self.url_database:
                    self.url_database.add_url(url, status="pending")

                html, failure_reason = await self.fetch(session, url, tor_session=tor_session)
                status = "visited"

                if html and self.parser:
                    links = self.parser.extract_links(html, url)
                    logger.info(f"{len(links)} links extracted from {url}")

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
                logger.info(f"Processed ({self._pages_crawled}): {url} [{status}]")

                if self.max_pages and self._pages_crawled >= self.max_pages:
                    logger.info("Reached max pages limit, stopping crawler")
                    self._stop_event.set()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Worker error for {url}: {e}")

            finally:
                self.queue.task_done()

    async def scheduler(self):
        """Scheduler task that feeds URLs from the frontier into the work queue."""

        idle_loops = 0

        while not self._stop_event.is_set():
            url = self.frontier.get_next_url()

            if url:
                idle_loops = 0
                await self.queue.put(url)
                continue

            # If the queue is empty and there are no pending URLs, stop after a short idling period.
            if self.queue.empty():
                idle_loops += 1
                if idle_loops >= 10:  # ~5 seconds of idle
                    logger.info("No more URLs to crawl, stopping crawler")
                    self._stop_event.set()
                    break
            else:
                idle_loops = 0

            await asyncio.sleep(0.5)

    async def run(self):
        """Run the crawler until stopped or until the stop condition is met."""

        connector = aiohttp.TCPConnector(limit=self.concurrency)
        tor_proxy_url = self.tor_proxy.replace("socks5h://", "socks5://", 1)
        tor_connector = ProxyConnector.from_url(tor_proxy_url, limit=self.concurrency)

        async with aiohttp.ClientSession(connector=connector) as session, aiohttp.ClientSession(connector=tor_connector) as tor_session:
            workers = [
                asyncio.create_task(self.worker(session, tor_session=tor_session))
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

                logger.info(
                    "Crawler finished: processed={} failed={} pending_frontier={}",
                    self._pages_crawled,
                    self._pages_failed,
                    self.frontier.pending_count(),
                )
