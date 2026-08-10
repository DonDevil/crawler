"""High-level crawler manager that ties together discovery, frontier, and crawling."""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from loguru import logger

from core.config import Config, load_config
from core.frontier import FrontierUnavailable
from core.frontier_executor import AsyncFrontier
from core.url_frontier import URLFrontier
from core.redis_frontier import RedisURLFrontier
from crawler.async_crawler import AsyncCrawler
from crawler.http_crawler import HTTPCrawler
from crawler.hybrid_crawler import HybridCrawler
from crawler.playwright_crawler import PlaywrightCrawler
from crawler.scrapling_crawler import ScraplingCrawler
from crawler.selenium_crawler import SeleniumCrawler
from crawler.tor_crawler import TorCrawler
from discovery.piracy_site_seeds import load_seeds
from discovery.search_engine_discovery import discover_urls_from_queries_with_report, get_engine_names_for_scope
from parsers.html_link_extractor import HTMLLinkExtractor
from storage.domain_database import DomainDatabase
from storage.media_evidence_store import MediaEvidenceStore
from storage.redis_media_evidence_store import RedisMediaEvidenceStore
from storage.sqlite_media_evidence_store import SQLiteMediaEvidenceStore
from storage.url_database import URLDatabase
from utils.logger import configure_logging
from utils.url_utils import URLUtils

# Bounded retry for a single URL during startup seeding (`prepare_frontier`
# runs synchronously before any crawl worker/task exists -- see
# core/frontier_executor.py's AsyncFrontier docstring -- so a short blocking
# `time.sleep` here does not compete with worker I/O). Deliberately small:
# this absorbs a brief Redis blip, it does not hide a sustained outage --
# see docs/architecture/frontier-redis-failure-semantics.md, "Startup
# seeding failure semantics".
_SEED_ADD_MAX_ATTEMPTS = 3
_SEED_ADD_RETRY_DELAY_SECONDS = 0.5

# After this many consecutive URLs exhaust their retries within one loader
# call, stop attempting further per-URL retries for the remainder of that
# call. Without this, a sustained outage would multiply its cost by the
# size of the seed list (a 10K-URL seed file would burn ~1s/URL retrying a
# Redis that isn't coming back). Remaining URLs are still recorded via the
# same durable fallback, just without spending the retry budget on each one.
_SEED_ADD_CIRCUIT_BREAKER_THRESHOLD = 3


def build_media_evidence_store(config: Config) -> Optional[MediaEvidenceStore]:
    """Construct the configured media evidence backend, or `None` if media
    evidence is disabled entirely.

    Module-level (not a `CrawlerManager` method) so `main.py`'s CLI stub
    (`--claim-fingerprint-job`/`--complete-fingerprint-job`) can select the
    same backend without constructing a full `CrawlerManager` (crawler
    engine, discovery, frontier, ...).

    Deliberately does NOT mirror the frontier's redis-unavailable ->
    sqlite fallback: a production Redis outage for media evidence must be
    visible as a startup failure, not silently degrade to a different
    backend (docs/architecture/media-evidence-step1.md, "Backend
    selection"). If `media_evidence.type` is "redis" and Redis is
    unreachable, this raises and the caller's construction fails.
    """
    if not config.crawler.storage.enable_media_evidence:
        return None

    media_evidence_config = config.crawler.media_evidence
    if media_evidence_config.type.lower() == "redis":
        store: MediaEvidenceStore = RedisMediaEvidenceStore(
            redis_host=media_evidence_config.redis_host,
            redis_port=media_evidence_config.redis_port,
            redis_db=media_evidence_config.redis_db,
            namespace=media_evidence_config.redis_namespace,
            max_observations_per_asset=media_evidence_config.max_observations_per_asset,
            max_variants_per_asset=media_evidence_config.max_variants_per_asset,
            fingerprint_lease_ttl=media_evidence_config.fingerprint_lease_ttl,
            max_retries=media_evidence_config.max_retries,
            base_backoff=media_evidence_config.base_backoff,
            max_backoff=media_evidence_config.max_backoff,
            reclaim_batch_size=media_evidence_config.reclaim_batch_size,
            confirmed_match_stream_maxlen=media_evidence_config.confirmed_match_stream_maxlen,
        )
        logger.info(
            f"Using Redis media evidence store at {media_evidence_config.redis_host}:"
            f"{media_evidence_config.redis_port}/{media_evidence_config.redis_db} "
            f"ns={media_evidence_config.redis_namespace}"
        )
        return store

    store = SQLiteMediaEvidenceStore(
        path=config.crawler.storage.media_sqlite_path,
        fingerprint_lease_ttl=media_evidence_config.fingerprint_lease_ttl,
        max_retries=media_evidence_config.max_retries,
        base_backoff=media_evidence_config.base_backoff,
        max_backoff=media_evidence_config.max_backoff,
    )
    logger.info(f"Using SQLite media evidence store at {config.crawler.storage.media_sqlite_path}")
    return store


class CrawlerManager:
    """Top-level crawler coordinator."""

    def __init__(
        self,
        config: Optional[Config] = None,
        extra_seed_files: Optional[list[str]] = None,
        queries: Optional[list[str]] = None,
        include_seed_files: bool = True,
        resume_unfinished: bool = False,
        query_scope: str | None = None,
        crawl_engine: str | None = None,
        ignore_blacklist: bool = False,
    ):
        self.config = config or load_config()
        configure_logging("INFO")
        URLUtils.set_blacklist_enabled(not ignore_blacklist)

        self.url_database = URLDatabase(path=self.config.crawler.storage.sqlite_path)
        self.domain_database = DomainDatabase(path=self.config.crawler.storage.sqlite_path)
        self.media_database: Optional[MediaEvidenceStore] = build_media_evidence_store(self.config)

        # Initialize frontier based on config
        frontier_config = self.config.crawler.frontier
        frontier_type = frontier_config.type.lower()
        if frontier_type == "redis":
            try:
                self.frontier = RedisURLFrontier(
                    redis_host=frontier_config.redis_host,
                    redis_port=frontier_config.redis_port,
                    redis_db=frontier_config.redis_db,
                    rate_limit=self.config.crawler.rate_limit,
                    namespace=frontier_config.redis_namespace,
                    max_retries=frontier_config.max_retries,
                    base_backoff=frontier_config.base_backoff,
                    max_backoff=frontier_config.max_backoff,
                    lease_ttl=frontier_config.lease_ttl,
                    domain_scan_limit=frontier_config.domain_scan_limit,
                    reclaim_batch_size=frontier_config.reclaim_batch_size,
                )
                logger.info(
                    f"Using Redis frontier at {frontier_config.redis_host}:"
                    f"{frontier_config.redis_port}/{frontier_config.redis_db}"
                )
            except Exception as e:
                logger.warning(
                    f"Redis frontier unavailable at {frontier_config.redis_host}:"
                    f"{frontier_config.redis_port}: {e}. "
                    f"Falling back to SQLite (single-worker mode). "
                    f"To use multi-worker mode, ensure Redis is running and config.yaml is updated."
                )
                self.frontier = URLFrontier(
                    rate_limit=self.config.crawler.rate_limit,
                    url_database=self.url_database,
                    max_retries=frontier_config.max_retries,
                    base_backoff=frontier_config.base_backoff,
                    max_backoff=frontier_config.max_backoff,
                    lease_ttl=frontier_config.lease_ttl,
                )
        else:
            self.frontier = URLFrontier(
                rate_limit=self.config.crawler.rate_limit,
                url_database=self.url_database,
                max_retries=frontier_config.max_retries,
                base_backoff=frontier_config.base_backoff,
                max_backoff=frontier_config.max_backoff,
                lease_ttl=frontier_config.lease_ttl,
            )
            logger.info("Using SQLite frontier (single-worker mode)")

        # Non-blocking boundary for the recovery task below -- see
        # core/frontier_executor.py and docs/architecture/frontier-step4.md.
        # `self.frontier` itself stays the raw synchronous object: it's used
        # directly by prepare_frontier() (sync, startup-only, before any
        # concurrent event-loop work exists) and by existing callers/tests
        # that expect synchronous access.
        self.async_frontier = AsyncFrontier(self.frontier)

        self.link_extractor = HTMLLinkExtractor()

        selected_engine = (crawl_engine or self.config.crawler.engine or "async").lower()
        crawler_args = {
            "frontier": self.frontier,
            "parser": self.link_extractor,
            "concurrency": self.config.crawler.concurrency,
            "timeout": self.config.crawler.timeout,
            "max_retries": 3,
            "max_pages": self.config.crawler.max_pages,
            "user_agent": self.config.crawler.user_agent,
            "url_database": self.url_database,
            "media_database": self.media_database,
            "scrapling_enabled": self.config.crawler.scrapling_enabled,
            "scrapling_headless": self.config.crawler.scrapling_headless,
            "scrapling_stealth": self.config.crawler.scrapling_stealth,
            "scrapling_network_idle": self.config.crawler.scrapling_network_idle,
            "heartbeat_interval": frontier_config.heartbeat_interval,
        }

        hybrid_args = dict(crawler_args)

        basic_crawler_args = {
            key: value for key, value in crawler_args.items()
            if key not in {"scrapling_enabled", "scrapling_headless", "scrapling_stealth", "scrapling_network_idle"}
        }

        if selected_engine == "auto":
            self._crawler = HybridCrawler(**hybrid_args)
        elif selected_engine == "async":
            self._crawler = AsyncCrawler(**basic_crawler_args)
        elif selected_engine == "http":
            self._crawler = HTTPCrawler(**basic_crawler_args)
        elif selected_engine == "tor":
            self._crawler = TorCrawler(**basic_crawler_args)
        elif selected_engine == "playwright":
            self._crawler = PlaywrightCrawler(**basic_crawler_args)
        elif selected_engine == "selenium":
            self._crawler = SeleniumCrawler(**basic_crawler_args)
        elif selected_engine == "scrapling":
            self._crawler = ScraplingCrawler(
                **basic_crawler_args,
                headless=self.config.crawler.scrapling_headless,
                use_stealth=self.config.crawler.scrapling_stealth,
                network_idle=self.config.crawler.scrapling_network_idle,
            )
        else:
            raise ValueError(
                f"Unsupported crawler engine: {selected_engine}. "
                "Expected one of: auto, async, http, tor, playwright, selenium, scrapling"
            )

        self.crawl_engine = selected_engine

        self.extra_seed_files = extra_seed_files or []
        self.queries = queries or []
        self.include_seed_files = include_seed_files
        self.resume_unfinished = resume_unfinished
        self.query_scope = query_scope
        self._recovery_task: Optional[asyncio.Task] = None
        self._peak_active_domains: int = 0

        logger.info(f"Using crawler engine: {self.crawl_engine}")

    def clear_storage(self) -> None:
        """Clear all persisted crawl state from the SQLite storage."""

        counts_before = self.url_database.get_status_counts()
        self.url_database.clear()
        self.domain_database.clear()
        if self.media_database:
            self.media_database.clear()
        logger.info(f"Cleared crawl storage. Previous counts: {counts_before}")

    def set_max_pages(self, max_pages: int | None) -> None:
        """Override the active crawler page limit, or clear it for autonomous runs."""

        self._crawler.max_pages = max_pages

    def _priority_for_seed_url(self, url: str) -> int:
        return 8 if URLUtils.is_onion_url(url) else 12

    def _priority_for_unfinished_url(self, url: str, status: str) -> int:
        base_priority = 3 if status == "pending" else 6
        if URLUtils.is_onion_url(url):
            base_priority = max(0, base_priority - self.config.search.onion_priority_boost)
        return base_priority

    def _make_seed_url_adder(self) -> Callable[[str, int, str], str]:
        """Build a stateful per-loader-call function that adds one URL to
        the frontier during startup seeding, with bounded retry and a
        circuit breaker for a sustained Redis outage.

        A `FrontierUnavailable` here must never mean "URL handled" (the
        Step 7 failure contract) and must never abort the whole seeding
        pass over one transient blip either (that would lose every URL
        still left to load, not just this one). Instead: retry a few times,
        and if the frontier still can't take it, persist the URL directly
        to `url_database` with status "queued" -- the same status
        `load_unfinished_urls` already looks for, so the URL is
        automatically retried by a later `--unfinished` run rather than
        silently lost. See docs/architecture/frontier-redis-failure-semantics.md.

        Returns one of:
        - "accepted": queued into the frontier normally.
        - "rejected": an ordinary duplicate/blacklisted URL -- unrelated to
          Redis health, frontier.add_url() returned False without raising.
        - "deferred": the frontier was unavailable; the URL was instead
          recorded in storage for retry via `--unfinished`.
        """
        consecutive_unavailable = 0

        def add(url: str, priority: int, source_query: str = "") -> str:
            nonlocal consecutive_unavailable

            if consecutive_unavailable < _SEED_ADD_CIRCUIT_BREAKER_THRESHOLD:
                for attempt in range(1, _SEED_ADD_MAX_ATTEMPTS + 1):
                    try:
                        accepted = self.frontier.add_url(url, priority=priority, source_query=source_query)
                        consecutive_unavailable = 0
                        return "accepted" if accepted else "rejected"
                    except FrontierUnavailable as e:
                        if attempt < _SEED_ADD_MAX_ATTEMPTS:
                            logger.warning(
                                f"Frontier unavailable adding seed URL {url} "
                                f"(attempt {attempt}/{_SEED_ADD_MAX_ATTEMPTS}), retrying: {e}"
                            )
                            time.sleep(_SEED_ADD_RETRY_DELAY_SECONDS)
                        else:
                            logger.error(
                                f"Frontier unavailable adding seed URL {url} after "
                                f"{_SEED_ADD_MAX_ATTEMPTS} attempts; deferring to storage "
                                f"for retry via --unfinished: {e}"
                            )
                consecutive_unavailable += 1
            else:
                logger.warning(
                    f"Frontier still unavailable after {_SEED_ADD_CIRCUIT_BREAKER_THRESHOLD} "
                    f"consecutive failures; deferring {url} to storage without retrying"
                )

            self.url_database.add_url(url, status="queued")
            return "deferred"

        return add

    def _log_deferred_urls(self, deferred: int, total: int, what: str) -> None:
        if not deferred:
            return
        logger.error(
            f"{deferred}/{total} {what} could not be queued because the frontier "
            "was unavailable; they were recorded in storage and will be picked up "
            "by a subsequent run with --unfinished once the frontier recovers"
        )

    def load_seed_urls(self) -> None:
        """Load seed URLs from seed files and add them to the frontier."""
        files = list(self.config.crawler.seed_files) + self.extra_seed_files
        loaded = 0
        accepted = 0
        deferred = 0
        add_url = self._make_seed_url_adder()
        for seed_file in files:
            for url in load_seeds(seed_file):
                loaded += 1
                outcome = add_url(url, self._priority_for_seed_url(url))
                if outcome == "accepted":
                    accepted += 1
                elif outcome == "deferred":
                    deferred += 1

        logger.info(f"Loaded {accepted}/{loaded} seed URLs from {len(files)} file(s)")
        self._log_deferred_urls(deferred, loaded, "seed URLs")

    def load_unfinished_urls(self) -> None:
        """Load queued and pending URLs from the database into the frontier."""

        unfinished_urls = self.url_database.get_urls_and_statuses(["queued", "pending"])
        accepted = 0
        deferred = 0
        add_url = self._make_seed_url_adder()
        for url, status in unfinished_urls:
            priority = self._priority_for_unfinished_url(url, status)
            outcome = add_url(url, priority)
            if outcome == "accepted":
                accepted += 1
            elif outcome == "deferred":
                deferred += 1

        logger.info(f"Loaded {accepted}/{len(unfinished_urls)} unfinished URLs from storage")
        self._log_deferred_urls(deferred, len(unfinished_urls), "unfinished URLs")

    def prepare_frontier(self) -> None:
        """Populate the frontier according to the selected startup mode."""

        if self.resume_unfinished:
            self.load_unfinished_urls()
            return

        if self.include_seed_files:
            self.load_seed_urls()

        self.load_search_query_urls()

    def load_search_query_urls(self) -> None:
        """Use search queries to discover new seed URLs."""
        if not self.queries:
            return

        engine_names = get_engine_names_for_scope(
            self.query_scope,
            self.config.search.enabled_engines,
        )

        logger.info(
            "Running query discovery with scope={} engines={}",
            self.query_scope or "all",
            engine_names,
        )

        report = discover_urls_from_queries_with_report(
            self.queries,
            max_results=self.config.search.max_results_per_engine,
            engine_names=engine_names,
            timeout=self.config.search.timeout,
            user_agent=self.config.crawler.user_agent,
            engine_priorities=self.config.search.engine_priorities,
            onion_priority_boost=self.config.search.onion_priority_boost,
            blocked_engine_cooldown_queries=self.config.search.blocked_engine_cooldown_queries,
        )

        for query_report in report.query_reports:
            logger.info(
                "Discovery for query {!r}: {} unique URLs, engine hits={}, engine errors={}, skipped={}",
                query_report.query,
                len(query_report.urls),
                query_report.engine_results,
                query_report.engine_errors,
                query_report.skipped_engines,
            )

        discovered_items = report.discovered_items
        if not discovered_items and report.urls:
            discovered_items = [
                type("DiscoveryFallback", (), {"url": url, "priority": self._priority_for_seed_url(url)})
                for url in report.urls
            ]

        accepted = 0
        deferred = 0
        add_url = self._make_seed_url_adder()
        for item in discovered_items:
            source_query = getattr(item, "source_query", "")
            outcome = add_url(item.url, item.priority, source_query)
            if outcome == "accepted":
                accepted += 1
            elif outcome == "deferred":
                deferred += 1

        logger.info(
            "Loaded {}/{} unique URLs from search discovery after blacklist and dedupe filtering",
            accepted,
            len(report.urls),
        )
        self._log_deferred_urls(deferred, len(discovered_items), "discovered URLs")

    async def _sample_domain_scan_telemetry(self) -> None:
        """One cheap, read-only `domain_scan_limit` (K) visibility-risk
        sample -- see `RedisURLFrontier.get_domain_scan_telemetry` and
        docs/architecture/domain-scan-limit-decision.md. A no-op for
        backends without it (the local frontier). Never raises: this is
        diagnostic only and must not be able to affect the recovery loop's
        own reliability.
        """
        try:
            telemetry = await self.async_frontier.get_domain_scan_telemetry()
        except Exception as e:
            logger.debug(f"Domain scan telemetry sample failed (non-fatal): {e}")
            return

        if telemetry is None:
            return

        self._peak_active_domains = max(self._peak_active_domains, telemetry["active_domains"])
        if telemetry["exceeds_domain_scan_limit"]:
            logger.warning(
                f"Active domains ({telemetry['active_domains']}) exceed domain_scan_limit "
                f"({telemetry['domain_scan_limit']}) -- some domains may be invisible to "
                "scheduling; see docs/architecture/domain-scan-window-design.md"
            )
        else:
            logger.debug(
                f"Domain scan telemetry: active_domains={telemetry['active_domains']} "
                f"domain_scan_limit={telemetry['domain_scan_limit']}"
            )

    async def _recovery_loop(self) -> None:
        """Periodically reclaim abandoned inflight claims and promote due
        retries (docs/architecture/frontier-adr.md §7), and sample
        domain_scan_limit visibility-risk telemetry (see
        `_sample_domain_scan_telemetry`) on the same cadence -- deliberately
        piggybacked here rather than on a separate loop or the per-claim
        path, so this stays cheap and low-frequency by construction (see
        docs/architecture/domain-scan-limit-decision.md).

        Runs independently of whether the scheduler is polling, so it keeps
        sweeping even when the queue is fully drained except for one
        crashed worker's orphaned claim. Only started when the active
        frontier implements `reclaim_and_promote` (Redis today) -- the
        local frontier already promotes due retries lazily inside its own
        `get_next_url()` (ADR §10) and has nothing for this loop to do.
        Uses `self.async_frontier` so the (potentially blocking, Redis)
        call never runs directly on the event loop thread -- see
        core/frontier_executor.py.
        """
        interval = self.config.crawler.frontier.recovery_interval
        batch_size = self.config.crawler.frontier.reclaim_batch_size

        while True:
            try:
                reclaimed, requeued = await self.async_frontier.reclaim_and_promote(batch_size)
                if reclaimed or requeued:
                    logger.debug(f"Recovery sweep: reclaimed={reclaimed} requeued={requeued}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Recovery sweep failed: {e}")
            await self._sample_domain_scan_telemetry()
            await asyncio.sleep(interval)

    async def run(self):
        """Run the crawler until it completes or is stopped."""
        self.prepare_frontier()

        logger.info("Starting crawler")

        frontier_config = self.config.crawler.frontier
        self._recovery_task: Optional[asyncio.Task] = None
        if frontier_config.recovery_enabled and hasattr(self.frontier, "reclaim_and_promote"):
            self._recovery_task = asyncio.create_task(self._recovery_loop())
            logger.info(
                f"Started frontier recovery task (interval={frontier_config.recovery_interval}s, "
                f"batch_size={frontier_config.reclaim_batch_size})"
            )

        try:
            await self._crawler.run()
        except asyncio.CancelledError:
            logger.info("Crawler was cancelled")
        except Exception as exc:
            logger.exception(f"Crawler encountered an error: {exc}")
        finally:
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                await asyncio.gather(self._recovery_task, return_exceptions=True)

            logger.info("Crawler stopped")
            logger.info(f"Database status counts: {self.url_database.get_status_counts()}")
            if self._peak_active_domains:
                logger.info(
                    f"Peak active domains observed: {self._peak_active_domains} "
                    f"(domain_scan_limit={frontier_config.domain_scan_limit})"
                )
            if hasattr(self.frontier, "close"):
                self.frontier.close()
            self.url_database.close()
            self.domain_database.close()
            if self.media_database:
                self.media_database.close()


if __name__ == "__main__":
    # allow running directly for quick smoke tests
    manager = CrawlerManager()
    asyncio.run(manager.run())
