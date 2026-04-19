"""Entry point for the Anti-Piracy Crawler."""

from __future__ import annotations

import argparse
import asyncio
import json

from core.crawler_manager import CrawlerManager
from storage.domain_database import DomainDatabase
from storage.media_evidence_database import MediaEvidenceDatabase


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
        "--indefinite-run",
        dest="indefinite_run",
        action="store_true",
        help="Disable the page cap and keep crawling until all reachable URLs are visited and no new links are found.",
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
    parser.add_argument(
        "--claim-sample-job",
        action="store_true",
        help="Claim the next pending media sample job for the future fingerprinter service.",
    )
    parser.add_argument(
        "--worker-name",
        default="fingerprinter-worker",
        help="Worker name to use when claiming a sample job.",
    )
    parser.add_argument(
        "--mark-match",
        type=int,
        help="Mark a media asset ID as matched and increase the source site's priority score.",
    )
    parser.add_argument(
        "--match-title",
        help="Human-readable title for the confirmed matched media asset.",
    )
    parser.add_argument(
        "--match-confidence",
        type=float,
        default=1.0,
        help="Confidence score for a confirmed matched media asset.",
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

    if args.claim_sample_job or args.mark_match is not None:
        media_db = MediaEvidenceDatabase()
        try:
            if args.claim_sample_job:
                job = media_db.claim_next_sample_job(worker_name=args.worker_name)
                print(json.dumps(job or {}, indent=2, sort_keys=True))
                return

            if args.mark_match is not None:
                domain_db = DomainDatabase()
                try:
                    domain = media_db.mark_asset_matched(
                        args.mark_match,
                        matched_title=args.match_title or "Matched media",
                        confidence=args.match_confidence,
                        domain_database=domain_db,
                        score_increment=2.0,
                    )
                    print(json.dumps({
                        "asset_id": args.mark_match,
                        "matched_domain": domain,
                        "confidence": args.match_confidence,
                    }, indent=2, sort_keys=True))
                    return
                finally:
                    domain_db.close()
        finally:
            media_db.close()

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

    if args.indefinite_run:
        manager.set_max_pages(None)
    elif args.max_pages is not None:
        manager.set_max_pages(args.max_pages)

    if args.debug:
        # This is a quick way to bump logging level.
        from utils.logger import configure_logging

        configure_logging("DEBUG")

    asyncio.run(manager.run())


if __name__ == "__main__":
    main()
