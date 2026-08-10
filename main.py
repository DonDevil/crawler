"""Entry point for the Anti-Piracy Crawler."""

from __future__ import annotations

import argparse
import asyncio
import json

from core.config import load_config
from core.crawler_manager import CrawlerManager, build_media_evidence_store
from storage.media_evidence_store import FingerprintResult


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
        "--media-backend",
        choices=["sqlite", "redis"],
        help="Override config.yaml's crawler.media_evidence.type for this run.",
    )
    parser.add_argument(
        "--claim-fingerprint-job",
        action="store_true",
        help="Claim the next queued fingerprint job for the future fingerprinter service "
        "and print it (including its claim token) as JSON.",
    )
    parser.add_argument(
        "--worker-name",
        default="fingerprinter-worker",
        help="Worker id to use when claiming a fingerprint job.",
    )
    parser.add_argument(
        "--complete-fingerprint-job",
        dest="complete_asset_id",
        metavar="ASSET_ID",
        help="Complete a previously claimed fingerprint job for this asset id "
        "(requires --claim-token from a prior --claim-fingerprint-job call).",
    )
    parser.add_argument(
        "--claim-token",
        help="Claim token to present when completing a fingerprint job.",
    )
    parser.add_argument(
        "--decision",
        choices=["confirmed", "rejected", "uncertain"],
        default="confirmed",
        help="Aggregate fingerprint decision to record when completing a job.",
    )
    parser.add_argument(
        "--match-title",
        help="Human-readable title for a confirmed matched media asset.",
    )
    parser.add_argument(
        "--match-confidence",
        type=float,
        default=1.0,
        help="Confidence score to record when completing a fingerprint job.",
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

    if args.claim_fingerprint_job or args.complete_asset_id is not None:
        config = load_config()
        if args.media_backend:
            config.crawler.media_evidence.type = args.media_backend

        media_store = build_media_evidence_store(config)
        if media_store is None:
            parser.error("Media evidence is disabled (crawler.storage.enable_media_evidence: false)")

        try:
            if args.claim_fingerprint_job:
                job = media_store.claim_next_fingerprint_job(worker_id=args.worker_name)
                print(json.dumps(job.__dict__ if job else {}, indent=2, sort_keys=True))
                return

            if args.complete_asset_id is not None:
                if not args.claim_token:
                    parser.error("--complete-fingerprint-job requires --claim-token")

                result = FingerprintResult(
                    aggregate_decision=args.decision,
                    confidence=args.match_confidence,
                    matched_title=args.match_title,
                    worker_id=args.worker_name,
                )
                completed = media_store.complete_fingerprint_job(
                    args.complete_asset_id, args.claim_token, result=result
                )
                print(json.dumps({
                    "asset_id": args.complete_asset_id,
                    "completed": completed,
                    "decision": args.decision,
                    "confidence": args.match_confidence,
                    "note": "confirmed_match event feedback to domain scoring is a future consumer "
                    "(docs/architecture/media-evidence-redis-design.md §19) -- not run by this CLI.",
                }, indent=2, sort_keys=True))
                return
        finally:
            media_store.close()

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
