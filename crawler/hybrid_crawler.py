"""Hybrid crawler that routes each URL to the best crawler engine."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Optional

import aiohttp
import httpx
from aiohttp_socks import ProxyConnector
from loguru import logger

from core.claim_heartbeat import ClaimLostError, resolve_heartbeat_interval, run_with_heartbeat
from core.crawler_router import CrawlerRouter
from core.failure_classifier import classify_failure, is_ambiguous
from core.frontier import Frontier, FrontierClaim, FrontierUnavailable
from core.frontier_executor import AsyncFrontier
from core.media_evidence_executor import AsyncMediaEvidence
from core.network_health import HealthController, NetworkHealthState
from core.url_frontier import URLFrontier
from crawler.async_crawler import AsyncCrawler
from crawler.http_crawler import HTTPCrawler
from crawler.playwright_crawler import PlaywrightCrawler
from crawler.scrapling_crawler import ScraplingCrawler
from crawler.selenium_crawler import SeleniumCrawler
from crawler.tor_crawler import TorCrawler
from storage.url_database import URLDatabase
from tor.proxy_config import get_default_tor_proxy
from utils.request_headers import get_default_headers
from utils.url_utils import URLUtils


class HybridCrawler:
    """Use one shared frontier and route each URL to the best fetch strategy."""

    def __init__(
        self,
        frontier: Frontier,
        parser=None,
        concurrency=25,
        timeout=15,
        max_retries=3,
        max_pages: Optional[int] = None,
        user_agent: Optional[str] = None,
        url_database: Optional[URLDatabase] = None,
        media_database=None,
        scrapling_enabled: bool = True,
        scrapling_headless: bool = True,
        scrapling_stealth: bool = True,
        scrapling_network_idle: bool = True,
        heartbeat_interval: Optional[float] = None,
        health: Optional[HealthController] = None,
    ):
        self.frontier = AsyncFrontier(frontier)
        # Process-local network-health detection (N2/N3, core/network_health.py).
        # `None` (default) means every health-integration branch below is a
        # no-op -- existing behavior is unchanged for any caller that
        # doesn't pass one (see docs/architecture/network-failure-handling-design.md).
        self.health = health
        self.parser = parser
        self.concurrency = max(1, min(concurrency, max_pages)) if max_pages else max(1, concurrency)
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
        # Non-blocking boundary for Media Evidence writes made from the
        # crawl-time hot path -- see core/media_evidence_executor.py and
        # docs/architecture/fetch-extractor-audit.md §8/§14. `None` stays
        # `None` so every existing `if not self.media_database` guard is
        # unaffected.
        self.media_database = AsyncMediaEvidence(media_database) if media_database is not None else None
        self.scrapling_enabled = scrapling_enabled
        self.router = CrawlerRouter(allow_scrapling=self.scrapling_enabled)

        self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue(maxsize=self.concurrency)
        self._stop_event = asyncio.Event()
        self._pages_crawled = 0
        self._pages_failed = 0
        self._active_workers = 0
        self._engine_counts: Counter[str] = Counter()

        self._direct_session: aiohttp.ClientSession | None = None
        self._tor_session: aiohttp.ClientSession | None = None
        self._httpx_client: httpx.AsyncClient | None = None
        self._httpx_tor_client: httpx.AsyncClient | None = None

        self._playwright_ready = False
        self._playwright_error: str | None = None
        self._playwright_lock = asyncio.Lock()
        self._selenium_checked = False
        self._selenium_ready = False
        self._selenium_error: str | None = None
        self._selenium_lock = asyncio.Lock()

        self._http_semaphore = asyncio.Semaphore(max(1, min(10, self.concurrency)))
        self._tor_semaphore = asyncio.Semaphore(max(1, min(5, self.concurrency)))
        self._scrapling_semaphore = asyncio.Semaphore(max(1, min(3, self.concurrency)))
        self._playwright_semaphore = asyncio.Semaphore(max(1, min(2, self.concurrency)))
        self._selenium_semaphore = asyncio.Semaphore(1)

        # Sub-engines wrap `frontier`/`media_database` themselves (each
        # backend wraps whatever it's given in its own __init__) -- pass the
        # raw objects here, not self.frontier/self.media_database, so
        # neither is wrapped twice. Both adapters are idempotent if this
        # ever changes, but staying explicit is clearer.
        common_args = {
            "frontier": frontier,
            "parser": self.parser,
            "concurrency": self.concurrency,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "max_pages": self.max_pages,
            "user_agent": self.user_agent,
            "url_database": self.url_database,
            "media_database": media_database,
            "heartbeat_interval": self.heartbeat_interval,
        }
        self._async_engine = AsyncCrawler(**common_args)
        self._http_engine = HTTPCrawler(**common_args)
        self._tor_engine = TorCrawler(**common_args)
        self._playwright_engine = PlaywrightCrawler(**common_args)
        self._selenium_engine = SeleniumCrawler(**common_args)
        self._scrapling_engine = ScraplingCrawler(
            **common_args,
            headless=scrapling_headless,
            use_stealth=scrapling_stealth,
            network_idle=scrapling_network_idle,
        )

    async def _ensure_playwright_ready(self) -> tuple[bool, Optional[str]]:
        if self._playwright_ready:
            return True, None
        if self._playwright_error:
            return False, self._playwright_error

        async with self._playwright_lock:
            if self._playwright_ready:
                return True, None
            if self._playwright_error:
                return False, self._playwright_error

            try:
                await self._playwright_engine._start_browser()
            except Exception as exc:
                self._playwright_error = str(exc)
                logger.warning(f"Disabling Playwright for this run: {self._playwright_error}")
                return False, self._playwright_error

            self._playwright_ready = True
            return True, None

    async def _ensure_selenium_ready(self) -> tuple[bool, Optional[str]]:
        if self._selenium_checked:
            return self._selenium_ready, self._selenium_error

        async with self._selenium_lock:
            if self._selenium_checked:
                return self._selenium_ready, self._selenium_error

            try:
                driver = await asyncio.to_thread(self._selenium_engine._make_driver)
                await asyncio.to_thread(driver.quit)
                self._selenium_ready = True
                self._selenium_error = None
            except Exception as exc:
                self._selenium_ready = False
                self._selenium_error = str(exc)
                logger.warning(f"Disabling Selenium for this run: {self._selenium_error}")
            finally:
                self._selenium_checked = True

            return self._selenium_ready, self._selenium_error

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
            ready, error = await self._ensure_selenium_ready()
            if not ready:
                return None, error or "Selenium unavailable"
            async with self._selenium_semaphore:
                return await self._selenium_engine.fetch(url)

        if engine_name == "scrapling":
            async with self._scrapling_semaphore:
                return await self._scrapling_engine.fetch(url)

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

    async def _run_engine_plan(self, url: str) -> tuple[Optional[str], Optional[str], list[str], str]:
        """Run the full engine-escalation chain for one claim's URL.

        A single claim can legitimately span several engine attempts
        (async -> playwright -> selenium, etc.) before a final outcome is
        known -- this whole chain is the unit of work wrapped by the claim
        heartbeat in `worker()`, not each individual engine fetch, matching
        the ADR's guidance that escalation must not surface as a
        frontier-level failure/new claim mid-chain.
        """
        plan = list(self.router.get_engine_plan(url))
        attempted: set[str] = set()
        attempt_chain: list[str] = []
        html: Optional[str] = None
        failure_reason: Optional[str] = None
        engine_used = "unknown"

        while plan:
            engine_used = plan.pop(0)
            if engine_used in attempted:
                continue

            attempted.add(engine_used)
            attempt_chain.append(engine_used)
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
                    if plan:
                        logger.info(f"Escalating {url} from {engine_used} to {plan[0]}: {failure_reason}")
                    continue
                break

            if self.health is not None and self.health.state == NetworkHealthState.OFFLINE:
                # N2 §7: once local connectivity is probe-confirmed OFFLINE,
                # further engine attempts (Playwright/Selenium/etc.) are
                # guaranteed-wasted work -- no new signal is learned by
                # trying another engine while the host has no route at all.
                # Stop escalating; the worker completes this claim via
                # mark_deferred instead of mark_failed.
                logger.info(
                    f"Short-circuiting engine escalation for {url}: network_health is OFFLINE"
                )
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
            if plan:
                logger.info(f"Escalating {url} from {engine_used} to {plan[0]}: {failure_reason}")

        return html, failure_reason, attempt_chain, engine_used

    def _log_completion(
        self,
        claim: FrontierClaim,
        url: str,
        category,
        final_outcome: Optional[str] = None,
        status: str = "visited",
    ) -> None:
        """Structured per-completion observability line (N2 §10): extends
        the existing loguru-based logging already used throughout this
        module -- no new metrics backend/subsystem.

        `final_outcome` is precomputed by the caller when it's already
        known unambiguously (deferred, skipped); otherwise it's inferred
        here from `claim.attempt` vs. the frontier's own authoritative
        `max_retries` (not `self.max_retries`, which can differ -- see
        `core/crawler_manager.py`), mirroring exactly the same comparison
        `_complete_claim_script`/`URLFrontier.mark_failed` use internally.
        """
        if final_outcome is None:
            if status == "visited":
                final_outcome = "visited"
            elif status == "skipped":
                final_outcome = "skipped"
            else:
                effective_max_retries = getattr(self.frontier.raw, "max_retries", self.max_retries)
                final_outcome = "retry_scheduled" if claim.attempt < effective_max_retries else "failed_permanent"

        consumed_retry_budget = final_outcome in ("retry_scheduled", "failed_permanent")

        logger.info(
            "completion url={} attempt={} failure_category={} consumed_retry_budget={} "
            "network_health_state={} host_identity={} timestamp={} final_outcome={}",
            url,
            claim.attempt,
            category.name if category is not None else "success",
            consumed_retry_budget,
            self.health.state.value if self.health is not None else "disabled",
            self.health.host_identity if self.health is not None else "unknown",
            time.time(),
            final_outcome,
        )

    async def worker(self):
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
                    self._log_completion(claim, url, category=None, final_outcome="skipped")
                    continue

                if self._sql_mode_mirror:
                    self.url_database.add_url(url, status="pending")

                (html, failure_reason, attempt_chain, engine_used), claim = await run_with_heartbeat(
                    self.frontier, claim, self._run_engine_plan(url), self.heartbeat_interval
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
                                discovered_by=engine_used,
                                discovery_method=media.get("detection_method", "parser"),
                                media_type=media.get("media_type"),
                                mime_type=media.get("mime_type"),
                                priority=max(0, URLUtils.get_link_priority(url, media["url"]) - 2),
                            )
                        except Exception as exc:
                            logger.debug(f"Skipping media evidence capture for {url}: {exc}")

                    for link in links:
                        await self.frontier.add_url(link, priority=URLUtils.get_link_priority(url, link))
                elif failure_reason:
                    status = "failed"
                    self._pages_failed += 1
                    logger.warning(f"Failed to crawl {url}: {failure_reason}")

                failure_category = None
                if status == "failed":
                    failure_category = classify_failure(failure_reason)
                    if self.health is not None and is_ambiguous(failure_category):
                        # Categories 2/3/5 only feed the trigger counter --
                        # they never by themselves exempt this (or any)
                        # claim's retry budget (N2 §5/§14). Whether *this*
                        # claim is exempt is decided below, purely from
                        # `self.health.state` read at completion time.
                        await self.health.record_ambiguous_failure()
                elif self.health is not None:
                    self.health.record_success()

                # N2 §6: the OFFLINE check is made at completion time, not
                # claim time -- a claim taken while HEALTHY can still
                # legitimately complete after the network dropped mid-fetch.
                offline_at_completion = (
                    status == "failed"
                    and self.health is not None
                    and self.health.state == NetworkHealthState.OFFLINE
                )

                if offline_at_completion:
                    await self.frontier.mark_deferred(claim, failure_reason or "")
                    if self._sql_mode_mirror:
                        self.url_database.update_status(url, "queued")
                elif status == "failed":
                    await self.frontier.mark_failed(claim, failure_reason or "")
                    if self._sql_mode_mirror:
                        self.url_database.update_status(url, status)
                else:
                    await self.frontier.mark_visited(claim)
                    if self._sql_mode_mirror:
                        self.url_database.update_status(url, status)

                self._log_completion(
                    claim,
                    url,
                    category=failure_category,
                    final_outcome="deferred" if offline_at_completion else None,
                    status=status,
                )

                self._pages_crawled += 1
                self._engine_counts[engine_used] += 1
                logger.info(
                    f"Processed ({self._pages_crawled}): {url} [{status}] via {engine_used} chain={' -> '.join(attempt_chain)}"
                )

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
            if self.health is not None and self.health.state == NetworkHealthState.OFFLINE:
                # N2 §7: pause new claims while confirmed OFFLINE -- skip
                # get_next_url entirely, and don't let the pause be
                # mistaken for "no more work" by the idle-shutdown check
                # below. Reuses the same 0.5s idle-poll sleep already used
                # elsewhere in this loop, so pausing is never a busy loop.
                idle_loops = 0
                await asyncio.sleep(0.5)
                continue

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

                if self.health is not None:
                    await self.health.aclose()

                logger.info(
                    "Hybrid crawler finished: processed={} failed={} pending_frontier={} engine_usage={}",
                    self._pages_crawled,
                    self._pages_failed,
                    await self.frontier.pending_count(),
                    dict(self._engine_counts),
                )
