#!/usr/bin/env python3
"""Crawl-run report: analyze the SQLite crawl-state database or the Redis
frontier's *current* keyspace and print a human-readable summary (and,
optionally, write a machine-readable JSON result).

The Redis path used to assume keys (`{ns}:urls:queued`, `{ns}:urls:failed`)
that no longer exist in the current frontier keyspace (core/redis_frontier.py)
-- see docs/architecture/frontier-adr.md. This tool now reads state
exclusively through `RedisURLFrontier.get_status_counts()` and the other
current, documented keys, and reports a metric as "N/A" rather than 0 when
it genuinely cannot be derived (e.g. run timing for an ad-hoc Redis snapshot
-- see `report_lib.redis_timing_unavailable`).

Examples:
  python tests/report.py --sql
  python tests/report.py --redis
  python tests/report.py --redis --namespace crawler --output results/report.json
  python tests/report.py --redis --run-json results/overnight.json   # merge in captured run metadata/timing/resources
"""

from __future__ import annotations

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report_lib  # noqa: E402


def _load_piracy_sites() -> list[str]:
    try:
        with open("seeds/piracy_sites.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        print("Warning: seeds/piracy_sites.txt not found, skipping piracy stats", file=sys.stderr)
        return []


def _frontier_kwargs_from_config(frontier_config) -> dict:
    return dict(
        max_retries=frontier_config.max_retries,
        base_backoff=frontier_config.base_backoff,
        max_backoff=frontier_config.max_backoff,
        lease_ttl=frontier_config.lease_ttl,
        domain_scan_limit=frontier_config.domain_scan_limit,
        reclaim_batch_size=frontier_config.reclaim_batch_size,
    )


def _build_metadata_from_config(config, backend: str, namespace_override: str | None) -> dict:
    frontier_cfg = config.crawler.frontier
    return {
        "generated_at": report_lib.now_iso(),
        "backend": backend,
        "redis": (
            {
                "host": frontier_cfg.redis_host,
                "port": frontier_cfg.redis_port,
                "db": frontier_cfg.redis_db,
                "namespace": namespace_override or frontier_cfg.redis_namespace,
            }
            if backend == "redis"
            else None
        ),
        "sqlite_path": config.crawler.storage.sqlite_path if backend == "sqlite" else None,
        "worker_count": None,
        "crawl_engine": None,
        "search_engines": None,
        "query_scope": None,
        "queries": None,
        "seed_files": None,
        "crawl_mode": None,
        "max_pages": None,
        "indefinite_run": None,
        "rate_limit": None,
        "config_defaults": {
            "concurrency": config.crawler.concurrency,
            "max_pages": config.crawler.max_pages,
            "rate_limit": config.crawler.rate_limit,
        },
        "note": (
            "Ad-hoc snapshot: per-run CLI parameters (query, seed files, crawl mode, worker count, "
            "search engines, surface/dark-web scope, indefinite-run) are not persisted by the frontier "
            "and are unavailable outside a live monitored run. 'config_defaults' reflects config.yaml, "
            "not necessarily the flags used for any particular past run. Capture a run's real metadata "
            "with `python main.py ... --monitor-resources --output <file>` and pass --run-json <file> "
            "here to merge it in."
        ),
    }


def _merge_run_json(report: dict, run_json_path: str) -> dict:
    with open(run_json_path, "r", encoding="utf-8") as f:
        captured = json.load(f)

    for key in ("metadata", "timing", "resources", "redis", "configuration"):
        if captured.get(key) is not None:
            report[key] = captured[key]

    # Recompute throughput/failures against the (possibly freshly-queried)
    # counts using the captured run's timing/worker_count, so a live counts
    # refresh combined with a captured duration still yields a correct rate.
    duration = report["timing"].get("duration_seconds")
    worker_count = report["metadata"].get("worker_count")
    snapshot_like = {
        "discovered_total": report["counts"].get("discovered_total"),
        "visited": report["counts"].get("visited"),
        "failed_permanent": report["counts"].get("failed_permanent"),
        "skipped": report["counts"].get("skipped"),
    }
    report["throughput"] = report_lib.compute_throughput(snapshot_like, duration, worker_count)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze crawler database - SQLite or Redis frontier",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--sql", action="store_true", help="Analyze SQLite database (default if neither flag specified)")
    parser.add_argument("--redis", action="store_true", help="Analyze Redis frontier instead of SQLite")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml (default %(default)s)")
    parser.add_argument("--namespace", default=None, help="Override the Redis namespace from config.yaml")
    parser.add_argument("--output", default=None, help="Write the machine-readable JSON report to this path")
    parser.add_argument(
        "--run-json",
        default=None,
        help="Merge in metadata/timing/resources captured by a prior "
        "`main.py --monitor-resources --output <file>` run",
    )
    parser.add_argument("--no-piracy-stats", action="store_true", help="Skip the piracy-site breakdown section")
    args = parser.parse_args()

    from core.config import load_config

    config = load_config(args.config)

    source = "redis" if args.redis else "sql"

    if source == "redis":
        frontier_cfg = config.crawler.frontier
        namespace = args.namespace or frontier_cfg.redis_namespace
        snapshot = report_lib.redis_snapshot(
            frontier_cfg.redis_host,
            frontier_cfg.redis_port,
            frontier_cfg.redis_db,
            namespace,
            frontier_kwargs=_frontier_kwargs_from_config(frontier_cfg),
        )
        if not snapshot.get("available"):
            print(f"Error connecting to Redis: {snapshot.get('error')}", file=sys.stderr)
            print("Make sure Redis is running and configured correctly.", file=sys.stderr)
            sys.exit(1)

        metadata = _build_metadata_from_config(config, "redis", namespace)
        timing = report_lib.redis_timing_unavailable(namespace)
    else:
        db_path = config.crawler.storage.sqlite_path
        snapshot = report_lib.sqlite_snapshot(db_path)
        metadata = _build_metadata_from_config(config, "sqlite", None)
        timing = report_lib.sqlite_timing(snapshot)

    report = report_lib.build_report(metadata=metadata, timing=timing, snapshot=snapshot)
    if source == "redis":
        report["snapshot_redis_info"] = snapshot.get("redis_info")
        report["counts"]["active_domains"] = snapshot.get("active_domains")

    if args.run_json:
        report = _merge_run_json(report, args.run_json)

    print(report_lib.render_human_report(report))

    if not args.no_piracy_stats:
        piracy_sites = _load_piracy_sites()
        if piracy_sites:
            if source == "redis":
                frontier_cfg = config.crawler.frontier
                namespace = args.namespace or frontier_cfg.redis_namespace
                stats = report_lib.redis_piracy_stats(
                    frontier_cfg.redis_host,
                    frontier_cfg.redis_port,
                    frontier_cfg.redis_db,
                    namespace,
                    piracy_sites,
                    frontier_kwargs=_frontier_kwargs_from_config(frontier_cfg),
                )
            else:
                stats = report_lib.sqlite_piracy_stats(config.crawler.storage.sqlite_path, piracy_sites)

            total_visited = sum(stats.get("visited", {}).values())
            total_failed = sum(stats.get("failed", {}).values())
            total_queued = sum(stats.get("queued", {}).values())
            total = report["counts"].get("discovered_total") or 0

            print("\n#Piracy Site Analytics\n")
            print(f"Total Visited Pirated Sites: {total_visited}")
            print(f"Total Failed Pirated Sites: {total_failed}")
            print(f"Total Queued Pirated Sites: {total_queued}")
            print()
            print(f"Visited Pirated Sites Percentage: {report_lib.fmt_pct(report_lib.safe_pct(total_visited, total))}")
            print(f"Failed Pirated Sites Percentage: {report_lib.fmt_pct(report_lib.safe_pct(total_failed, total))}")
            print(f"Queued Pirated Sites Percentage: {report_lib.fmt_pct(report_lib.safe_pct(total_queued, total))}")
            report["piracy_stats"] = stats

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nWrote JSON report to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
