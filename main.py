"""Entry point for the Anti-Piracy Crawler."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import load_config
from core.crawler_manager import CrawlerManager, build_media_evidence_store
from storage.media_evidence_store import FingerprintResult

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "benchmarks"))


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
    parser.add_argument(
        "--monitor-resources",
        action="store_true",
        help="Sample process CPU/RSS (and Redis INFO, if using the Redis frontier) on a "
        "background thread for the duration of the run, using the same "
        "tests/benchmarks/common.py:ResourceMonitor the benchmark suite uses, and include the "
        "results in the run report. Lightweight periodic sampling only -- never scans Redis "
        "keys, never blocks the crawler's event loop.",
    )
    parser.add_argument(
        "--monitor-interval",
        type=float,
        default=10.0,
        help="Seconds between resource samples when --monitor-resources is set (default %(default)s).",
    )
    parser.add_argument(
        "--output",
        help="Write a machine-readable JSON run report to this path when the crawl finishes "
        "(counts, timing, throughput, resources, Redis stats, run configuration). "
        "See tests/report_lib.py for the schema.",
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

    if not args.monitor_resources and not args.output:
        # No reporting requested -- run exactly as before, with zero added
        # overhead (no monitor thread, no post-run Redis/SQLite reconnect).
        asyncio.run(manager.run())
        return

    monitor = None
    if args.monitor_resources:
        from common import ResourceMonitor  # tests/benchmarks/common.py

        redis_conn = getattr(manager.frontier, "redis_conn", None)
        monitor = ResourceMonitor(redis_conn=redis_conn, interval=args.monitor_interval, include_children=True)

    # The frontier actually constructed may differ from config.yaml's
    # crawler.frontier.type: CrawlerManager falls back redis -> sqlite at
    # construction time if Redis was unreachable (core/crawler_manager.py).
    backend = "redis" if type(manager.frontier).__name__ == "RedisURLFrontier" else "sqlite"
    frontier_cfg = manager.config.crawler.frontier

    crawl_mode = "unfinished" if args.unfinished else "query-only" if args.query_only else "seeds+query"
    query_scope = "surface-web" if args.surface_web else "dark-web" if args.dark_web else None

    search_engines = None
    if args.queries:
        from discovery.search_engine_discovery import get_engine_names_for_scope

        search_engines = get_engine_names_for_scope(query_scope, manager.config.search.enabled_engines)

    seed_files = (
        list(manager.config.crawler.seed_files) + (args.seed_files or [])
        if not args.query_only and not args.unfinished
        else None
    )

    import report_lib  # tests/report_lib.py

    # Snapshot frontier state *before* this run adds/claims anything, so the
    # report can later isolate this run's activity from the frontier's
    # lifetime-cumulative state (the Redis namespace / SQLite db is not
    # reset between runs by design -- see report_lib.build_this_run).
    if backend == "redis":
        pre_run_snapshot = report_lib.redis_snapshot(
            frontier_cfg.redis_host, frontier_cfg.redis_port, frontier_cfg.redis_db, frontier_cfg.redis_namespace
        )
    else:
        sqlite_path = manager.config.crawler.storage.sqlite_path
        pre_run_snapshot = report_lib.sqlite_snapshot(sqlite_path) if os.path.exists(sqlite_path) else None

    start_dt = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()

    if monitor is not None:
        monitor.__enter__()
    try:
        asyncio.run(manager.run())
    finally:
        if monitor is not None:
            monitor.__exit__(None, None, None)

    end_dt = datetime.now(timezone.utc)
    duration_seconds = max(time.monotonic() - start_monotonic, 0.0)

    worker_count = getattr(manager._crawler, "concurrency", manager.config.crawler.concurrency)
    max_pages_effective = getattr(manager._crawler, "max_pages", manager.config.crawler.max_pages)

    metadata = {
        "generated_at": report_lib.now_iso(),
        "backend": backend,
        "redis": (
            {
                "host": frontier_cfg.redis_host,
                "port": frontier_cfg.redis_port,
                "db": frontier_cfg.redis_db,
                "namespace": frontier_cfg.redis_namespace,
            }
            if backend == "redis"
            else None
        ),
        "sqlite_path": manager.config.crawler.storage.sqlite_path if backend == "sqlite" else None,
        "worker_count": worker_count,
        "crawl_engine": manager.crawl_engine,
        "search_engines": search_engines,
        "query_scope": query_scope,
        "queries": args.queries,
        "seed_files": seed_files,
        "crawl_mode": crawl_mode,
        "max_pages": max_pages_effective,
        "indefinite_run": bool(args.indefinite_run),
        "rate_limit": manager.config.crawler.rate_limit,
        "note": (
            "backend reflects the frontier actually constructed for this run, which can differ "
            "from config.yaml's crawler.frontier.type if Redis was unreachable at startup and the "
            "crawler fell back to SQLite -- see core/crawler_manager.py."
        ),
    }

    timing = {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "duration_seconds": duration_seconds,
        "source": "process-wall-clock",
        "note": (
            "Actual wall-clock start (immediately before manager.run()) and end (immediately after "
            "it returned) of this process -- not derived from URL timestamps."
        ),
    }

    if backend == "redis":
        snapshot = report_lib.redis_snapshot(
            frontier_cfg.redis_host, frontier_cfg.redis_port, frontier_cfg.redis_db, frontier_cfg.redis_namespace
        )
    else:
        snapshot = report_lib.sqlite_snapshot(manager.config.crawler.storage.sqlite_path)

    if not snapshot.get("available", True):
        print(f"Warning: could not read final {backend} state: {snapshot.get('error')}", file=sys.stderr)

    resources = None
    redis_resources = None
    if monitor is not None:
        resources = report_lib.build_resource_report(monitor.samples, args.monitor_interval)
        if backend == "redis":
            redis_resources = report_lib.build_redis_resource_report(monitor.samples, args.monitor_interval)

    report = report_lib.build_report(
        metadata=metadata,
        timing=timing,
        snapshot=snapshot,
        resources=resources,
        redis_resources=redis_resources,
        configuration=manager.config.model_dump(),
        pre_run_snapshot=pre_run_snapshot,
    )

    print("\n" + report_lib.render_human_report(report))

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nWrote run report to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
