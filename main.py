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
        help="Search query string to discover URLs using the configured search engines.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Override max pages to crawl (default from config).",
    )
    parser.add_argument(
        "--crawler-engine",
        choices=["auto", "async", "http", "tor", "playwright", "selenium", "scrapling"],
        help="Crawler implementation to use for page fetching.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--clear-db",
        action="store_true",
        help="Clear the stored SQLite crawl state before starting.",
    )
    parser.add_argument(
        "--ignore-blacklist",
        action="store_true",
        help="Allow crawling domains listed in datasets/domain_blacklist.txt.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--query-only",
        action="store_true",
        help="Use query discovery only and skip configured seed files.",
    )
    mode_group.add_argument(
        "--unfinished",
        action="store_true",
        help="Resume queued and pending URLs from storage only.",
    )
    query_scope_group = parser.add_mutually_exclusive_group()
    query_scope_group.add_argument(
        "--surface-web",
        action="store_true",
        help="Use only surface-web search engines for query discovery.",
    )
    query_scope_group.add_argument(
        "--dark-web",
        action="store_true",
        help="Use only dark-web search engines for query discovery.",
    )

    args = parser.parse_args()

    manager = CrawlerManager(
        extra_seed_files=args.seed_files,
        queries=args.queries,
        include_seed_files=not args.query_only and not args.unfinished,
        resume_unfinished=args.unfinished,
        query_scope="surface-web" if args.surface_web else "dark-web" if args.dark_web else None,
        crawl_engine=args.crawler_engine,
        ignore_blacklist=args.ignore_blacklist,
    )

    if args.clear_db:
        manager.clear_storage()

    if args.max_pages is not None:
        manager.set_max_pages(args.max_pages)

    if args.debug:
        # This is a quick way to bump logging level.
        from utils.logger import configure_logging

        configure_logging("DEBUG")

    asyncio.run(manager.run())


if __name__ == "__main__":
    main()
