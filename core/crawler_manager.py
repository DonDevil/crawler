"""High-level crawler manager that ties together discovery, frontier, and crawling."""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from core.config import Config, load_config
from core.url_frontier import URLFrontier
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
from storage.url_database import URLDatabase
from utils.logger import configure_logging
from utils.url_utils import URLUtils


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

        self.frontier = URLFrontier(
            rate_limit=self.config.crawler.rate_limit,
            url_database=self.url_database,
        )

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
            "scrapling_enabled": self.config.crawler.scrapling_enabled,
            "scrapling_headless": self.config.crawler.scrapling_headless,
            "scrapling_stealth": self.config.crawler.scrapling_stealth,
            "scrapling_network_idle": self.config.crawler.scrapling_network_idle,
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

        logger.info(f"Using crawler engine: {self.crawl_engine}")

    def clear_storage(self) -> None:
        """Clear all persisted URL crawl state from the SQLite storage."""

        counts_before = self.url_database.get_status_counts()
        self.url_database.clear()
        logger.info(f"Cleared crawl storage. Previous counts: {counts_before}")

    def set_max_pages(self, max_pages: int) -> None:
        """Override the active crawler page limit."""

        self._crawler.max_pages = max_pages

    def _priority_for_seed_url(self, url: str) -> int:
        return 8 if URLUtils.is_onion_url(url) else 12

    def _priority_for_unfinished_url(self, url: str, status: str) -> int:
        base_priority = 3 if status == "pending" else 6
        if URLUtils.is_onion_url(url):
            base_priority = max(0, base_priority - self.config.search.onion_priority_boost)
        return base_priority

    def load_seed_urls(self) -> None:
        """Load seed URLs from seed files and add them to the frontier."""
        files = list(self.config.crawler.seed_files) + self.extra_seed_files
        loaded = 0
        for seed_file in files:
            for url in load_seeds(seed_file):
                self.frontier.add_url(url, priority=self._priority_for_seed_url(url))
                loaded += 1

        logger.info(f"Loaded {loaded} seed URLs from {len(files)} file(s)")

    def load_unfinished_urls(self) -> None:
        """Load queued and pending URLs from the database into the frontier."""

        unfinished_urls = self.url_database.get_urls_and_statuses(["queued", "pending"])
        for url, status in unfinished_urls:
            self.frontier.add_url(url, priority=self._priority_for_unfinished_url(url, status))

        logger.info(f"Loaded {len(unfinished_urls)} unfinished URLs from storage")

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

        for item in discovered_items:
            self.frontier.add_url(item.url, priority=item.priority)

        logger.info(f"Loaded {len(report.urls)} unique URLs from search discovery")

    async def run(self):
        """Run the crawler until it completes or is stopped."""
        self.prepare_frontier()

        logger.info("Starting crawler")
        try:
            await self._crawler.run()
        except asyncio.CancelledError:
            logger.info("Crawler was cancelled")
        except Exception as exc:
            logger.exception(f"Crawler encountered an error: {exc}")
        finally:
            logger.info("Crawler stopped")
            logger.info(f"Database status counts: {self.url_database.get_status_counts()}")
            self.url_database.close()


if __name__ == "__main__":
    # allow running directly for quick smoke tests
    manager = CrawlerManager()
    asyncio.run(manager.run())
