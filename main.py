"""Entry point for the Anti-Piracy Crawler."""

from __future__ import annotations

import argparse
import asyncio

from core.crawler_manager import CrawlerManager


def main() -> None:
    parser = argparse.ArgumentParser(description="Anti-Piracy Web Crawler")
    parser.add_argument(
        "--seed-file",
        dest="seed_files",
        action="append",
        help="Additional seed file(s) containing URLs to start from.",
    )
    parser.add_argument(
        "--query",
        dest="queries",
        action="append",
        help="Search query string to discover URLs (uses DuckDuckGo).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Override max pages to crawl (default from config).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    manager = CrawlerManager(
        extra_seed_files=args.seed_files,
        queries=args.queries,
    )

    if args.max_pages is not None:
        manager._crawler.max_pages = args.max_pages

    if args.debug:
        # This is a quick way to bump logging level.
        from utils.logger import configure_logging

        configure_logging("DEBUG")

    asyncio.run(manager.run())


if __name__ == "__main__":
    main()
