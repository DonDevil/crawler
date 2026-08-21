"""Optional Scrapling-powered crawler for anti-bot protected surface-web pages."""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from core.claim_heartbeat import ClaimLostError, resolve_heartbeat_interval, run_with_heartbeat
from core.frontier import Frontier, FrontierClaim, FrontierUnavailable
from core.frontier_executor import AsyncFrontier
from core.media_evidence_executor import AsyncMediaEvidence
from core.url_frontier import URLFrontier
from storage.media_evidence_store import MediaEvidenceUnavailable
from storage.url_database import URLDatabase
from utils.url_utils import URLUtils

try:
    from scrapling.fetchers import DynamicFetcher, StealthyFetcher
except Exception:  # pragma: no cover
    DynamicFetcher = None
    StealthyFetcher = None


class ScraplingCrawler:
    """Queue-driven crawler that uses Scrapling for stealthy browser-backed fetching."""

    def __init__(
        self,
        frontier: Frontier,
        parser=None,
        concurrency=10,
        timeout=30,
        max_retries=2,
        max_pages: Optional[int] = None,
        user_agent: Optional[str] = None,
        url_database: Optional[URLDatabase] = None,
        media_database=None,
        headless: bool = True,
        use_stealth: bool = True,
        network_idle: bool = True,
        heartbeat_interval: Optional[float] = None,
    ):
        self.frontier = AsyncFrontier(frontier)
        self.parser = parser
        self.concurrency = max(1, min(concurrency, 10, max_pages)) if max_pages else max(1, min(concurrency, 10))
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.heartbeat_interval = resolve_heartbeat_interval(
            heartbeat_interval, getattr(getattr(frontier, "raw", frontier), "lease_ttl", None)
        )
        self.user_agent = user_agent
        self.url_database = url_database
        # See docs/architecture/redis-sqlite-boundary-decision.md: mirroring
        # status into url_database only applies to the local frontier.
        self._sql_mode_mirror = url_database is not None and isinstance(self.frontier.raw, URLFrontier)
        # Non-blocking boundary for Media Evidence writes -- see
        # core/media_evidence_executor.py and
        # docs/architecture/fetch-extractor-audit.md §8/§14.
        self.media_database = AsyncMediaEvidence(media_database) if media_database is not None else None
        self.headless = headless
        self.use_stealth = use_stealth
        self.network_idle = network_idle

        self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue(maxsize=self.concurrency)
        self._stop_event = asyncio.Event()
        self._pages_crawled = 0
        self._pages_failed = 0
        self._active_workers = 0

    @property
    def available(self) -> bool:
        return StealthyFetcher is not None or DynamicFetcher is not None

    def _fetch_sync(self, url: str) -> tuple[Optional[str], Optional[str]]:
        fetcher_cls = StealthyFetcher if self.use_stealth else DynamicFetcher
        if fetcher_cls is None:
            return None, "Scrapling fetchers are not installed"

        page = fetcher_cls.fetch(
            url,
            headless=self.headless,
            network_idle=self.network_idle,
            disable_resources=True,
        )

        final_url = str(getattr(page, "url", url) or url)
        if URLUtils.is_suspicious_redirect(url, final_url):
            return None, f"Suspicious redirect to {final_url}"

        status = int(getattr(page, "status", 200) or 200)
        if status != 200:
            return None, f"HTTP {status}"

        html_content = getattr(page, "html_content", None)
        if html_content is not None:
            html = str(html_content)
        else:
            body = getattr(page, "body", b"") or b""
            html = body.decode("utf-8", errors="ignore") if isinstance(body, (bytes, bytearray)) else str(body)

        if not html.strip():
            return None, "Scrapling returned empty HTML"

        return html, None

    async def fetch(
        self,
        url: str,
        client=None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Fetch a page through Scrapling and return rendered HTML when available."""

        if URLUtils.is_onion_url(url):
            return None, "Scrapling fallback is intended for surface-web URLs only"

        last_error: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                return await asyncio.to_thread(self._fetch_sync, url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.warning(f"Scrapling fetch failed ({attempt}/{self.max_retries}) {url}: {exc}")
                await asyncio.sleep(1)

        return None, last_error or "unknown Scrapling error"

    async def worker(self, client=None):
        while not self._stop_event.is_set():
            claim = await self.queue.get()
            self._active_workers += 1
            url = claim.url if claim else None

            try:
                if claim is None:
                    continue

                if URLUtils.is_blacklisted(url):
                    logger.info(f"Skipping blacklisted URL during crawl: {url}")
                    await self.frontier.mark_skipped(claim)
                    if self._sql_mode_mirror:
                        self.url_database.update_status(url, "skipped")
                    continue

                if self._sql_mode_mirror:
                    self.url_database.add_url(url, status="pending")

                (html, failure_reason), claim = await run_with_heartbeat(
                    self.frontier, claim, self.fetch(url, client=client), self.heartbeat_interval
                )
                status = "visited"

                if html and self.parser:
                    parsed_content = (
                        self.parser.extract_content(html, url)
                        if hasattr(self.parser, "extract_content")
                        else {"links": self.parser.extract_links(html, url), "media_links": []}
                    )
                    links = parsed_content.get("links", set())
                    media_links = parsed_content.get("media_links", [])
                    for media in media_links:
                        if not self.media_database:
                            continue
                        try:
                            await self.media_database.record_media_link(
                                url=media["url"],
                                source_page=url,
                                referrer_url=url,
                                discovered_by="scrapling",
                                discovery_method=media.get("detection_method", "parser"),
                                media_type=media.get("media_type"),
                                mime_type=media.get("mime_type"),
                                priority=max(0, URLUtils.get_link_priority(url, media["url"]) - 2),
                            )
                        except MediaEvidenceUnavailable as exc:
                            logger.warning(f"Media evidence store unavailable, dropping candidate {media['url']}: {exc}")
                        except Exception as exc:
                            logger.debug(f"Skipping media evidence capture for {url}: {exc}")
                    for link in links:
                        await self.frontier.add_url(link, priority=URLUtils.get_link_priority(url, link))
                elif failure_reason:
                    status = "failed"
                    self._pages_failed += 1
                    logger.warning(f"Failed to crawl {url}: {failure_reason}")

                if status == "failed":
                    await self.frontier.mark_failed(claim, failure_reason or "")
                else:
                    await self.frontier.mark_visited(claim)
                if self._sql_mode_mirror:
                    self.url_database.update_status(url, status)

                self._pages_crawled += 1

                if self.max_pages and self._pages_crawled >= self.max_pages:
                    logger.info("Reached max pages limit, stopping crawler")
                    self._stop_event.set()

            except asyncio.CancelledError:
                if claim is not None:
                    try:
                        await self.frontier.mark_failed(claim, "worker cancelled")
                    except FrontierUnavailable as e:
                        logger.warning(
                            f"Frontier unavailable while abandoning cancelled claim for {url}: {e}"
                        )
                raise
            except ClaimLostError:
                logger.warning(
                    f"Claim lost for {url}: lease was reclaimed before this worker "
                    "finished (crashed-worker recovery or another owner); abandoning "
                    "without marking completion"
                )
            except FrontierUnavailable as e:
                logger.error(
                    f"Frontier unavailable while processing {url}: {e}; abandoning "
                    "without marking completion (lease-based recovery will retry once "
                    "the frontier is reachable again)"
                )
            except Exception as exc:
                logger.error(f"Worker error for {url}: {exc}")
                if claim is not None:
                    try:
                        await self.frontier.mark_failed(claim, str(exc))
                    except FrontierUnavailable as unavailable:
                        logger.error(
                            f"Frontier unavailable while recording failure for {url}: "
                            f"{unavailable}; abandoning without marking completion"
                        )
            finally:
                self._active_workers = max(0, self._active_workers - 1)
                self.queue.task_done()

    async def scheduler(self):
        idle_loops = 0

        while not self._stop_event.is_set():
            try:
                claim = await self.frontier.get_next_url()
            except FrontierUnavailable as e:
                logger.error(f"Frontier unavailable, will retry: {e}")
                claim = None

            if claim:
                idle_loops = 0
                await self.queue.put(claim)
                continue

            try:
                if self.queue.empty() and self._active_workers == 0 and not await self.frontier.has_pending():
                    idle_loops += 1
                    if idle_loops >= 10:
                        logger.info("No more URLs to crawl, stopping crawler")
                        self._stop_event.set()
                        break
                else:
                    idle_loops = 0
            except FrontierUnavailable as e:
                logger.error(f"Frontier unavailable while checking pending state, will retry: {e}")
                idle_loops = 0

            await asyncio.sleep(0.5)

    async def run(self):
        workers = [asyncio.create_task(self.worker()) for _ in range(self.concurrency)]
        scheduler_task = asyncio.create_task(self.scheduler())

        try:
            await self._stop_event.wait()
        finally:
            scheduler_task.cancel()
            for task in workers:
                task.cancel()

            await asyncio.gather(scheduler_task, *workers, return_exceptions=True)

            logger.info(
                "Scrapling crawler finished: processed={} failed={} pending_frontier={}",
                self._pages_crawled,
                self._pages_failed,
                await self.frontier.pending_count(),
            )
