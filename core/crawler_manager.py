"""High-level crawler manager that ties together discovery, frontier, and crawling."""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from core.config import Config, load_config
from core.url_frontier import URLFrontier
from crawler.async_crawler import AsyncCrawler
from discovery.piracy_site_seeds import load_seeds
from discovery.search_engine_discovery import discover_urls_from_queries
from parsers.html_link_extractor import HTMLLinkExtractor
from storage.url_database import URLDatabase
from utils.logger import configure_logging


class CrawlerManager:
    """Top-level crawler coordinator."""

    def __init__(
        self,
        config: Optional[Config] = None,
        extra_seed_files: Optional[list[str]] = None,
        queries: Optional[list[str]] = None,
    ):
        self.config = config or load_config()
        configure_logging("INFO")

        self.frontier = URLFrontier(rate_limit=self.config.crawler.rate_limit)

        self.url_database = URLDatabase(path=self.config.crawler.storage.sqlite_path)

        self.link_extractor = HTMLLinkExtractor()

        self._crawler = AsyncCrawler(
            frontier=self.frontier,
            parser=self.link_extractor,
            concurrency=self.config.crawler.concurrency,
            timeout=self.config.crawler.timeout,
            max_retries=3,
            max_pages=self.config.crawler.max_pages,
            user_agent=self.config.crawler.user_agent,
            url_database=self.url_database,
        )

        self.extra_seed_files = extra_seed_files or []
        self.queries = queries or []

    def load_seed_urls(self) -> None:
        """Load seed URLs from seed files and add them to the frontier."""
        files = list(self.config.crawler.seed_files) + self.extra_seed_files
        for seed_file in files:
            for url in load_seeds(seed_file):
                self.frontier.add_url(url)

    def load_search_query_urls(self) -> None:
        """Use search queries to discover new seed URLs."""
        if not self.queries:
            return

        discovered = discover_urls_from_queries(self.queries, max_results=20)
        for url in discovered:
            self.frontier.add_url(url)

    async def run(self):
        """Run the crawler until it completes or is stopped."""
        self.load_seed_urls()
        self.load_search_query_urls()

        logger.info("Starting crawler")
        try:
            await self._crawler.run()
        except asyncio.CancelledError:
            logger.info("Crawler was cancelled")
        except Exception as exc:
            logger.exception(f"Crawler encountered an error: {exc}")
        finally:
            logger.info("Crawler stopped")
            self.url_database.close()


if __name__ == "__main__":
    # allow running directly for quick smoke tests
    manager = CrawlerManager()
    asyncio.run(manager.run())
