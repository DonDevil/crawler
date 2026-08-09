#!/usr/bin/env python3
"""Multi-process distributed benchmark against a shared Redis frontier.

Launches N *independent OS processes* (via `multiprocessing.Process`, not
asyncio tasks in one process -- an in-process asyncio worker pool shares the
GIL and a single Redis connection pool in ways that can hide races a truly
distributed deployment would hit) each connecting to Redis on its own and
racing to claim/complete URLs from the same namespace.

Verifies, across all workers combined:
  - total claims issued and unique URLs claimed
  - duplicate successful claims (the same URL reported "visited" by more than
    one worker process -- would indicate a claim-safety violation)
  - completed / failed / retried counts
  - final frontier state (get_status_counts)
  - aggregate throughput

Examples:
    python tests/benchmarks/distributed_benchmark.py --workers 4 --urls 5000 --duration 20
    python tests/benchmarks/distributed_benchmark.py --workers 16 --urls 20000 --duration 30 \\
        --retry-rate 0.1 --domains 40
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workers", type=int, default=4, choices=[1, 2, 4, 8, 16],
                   help="Number of independent worker OS processes")
    p.add_argument("--urls", type=int, default=5000)
    p.add_argument("--duration", type=float, default=30.0,
                   help="Max seconds each worker process runs its claim/complete loop")
    p.add_argument("--retry-rate", type=float, default=0.0)
    p.add_argument("--domains", type=int, default=20)
    p.add_argument("--priority-distribution", default="fixed:10")
    p.add_argument("--process-time", type=float, default=0.0)
    p.add_argument("--poll-interval", type=float, default=0.01)
    p.add_argument("--max-idle-polls", type=int, default=500)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--monitor-interval", type=float, default=0.5)
    p.add_argument("--no-clear", action="store_true")
    common.add_common_frontier_args(p, default_namespace="bench_dist")
    return p.parse_args()


def _worker_main(
    worker_id: int, frontier_kwargs: dict, args_dict: dict, result_path: str, blacklist_path: str
) -> None:
    """Entry point for one independent worker process. Builds its own
    frontier connection (never shares one with the coordinator or siblings)
    and runs an isolated claim/complete loop, then writes its results as
    JSON to `result_path` for the coordinator to aggregate.

    `get_next_url()` re-checks the blacklist on every claim (see
    core/redis_frontier.py), so this process must point `URLUtils` at the
    same isolated blacklist file the coordinator used -- passed explicitly
    rather than relied upon via fork inheritance, so this is correct
    regardless of the multiprocessing start method.
    """

    common.URLUtils.set_blacklist_path(blacklist_path)
    frontier = common.build_frontier("redis", **frontier_kwargs)

    claims_issued = 0
    failed_attempts = 0
    success_urls: list[str] = []
    idle_polls = 0
    deadline = time.time() + args_dict["duration"]

    while time.time() < deadline:
        claim = frontier.get_next_url()
        if claim is None:
            if not frontier.has_pending():
                break
            idle_polls += 1
            if idle_polls > args_dict["max_idle_polls"]:
                break
            time.sleep(args_dict["poll_interval"])
            continue
        idle_polls = 0
        claims_issued += 1

        if args_dict["process_time"] > 0:
            time.sleep(args_dict["process_time"])

        if random.random() < args_dict["retry_rate"]:
            frontier.mark_failed(claim, "synthetic-distributed-benchmark-failure")
            failed_attempts += 1
        else:
            frontier.mark_visited(claim)
            success_urls.append(claim.url)

    frontier.close()

    Path(result_path).write_text(json.dumps({
        "worker_id": worker_id,
        "pid_reported_by_worker": True,
        "claims_issued": claims_issued,
        "failed_attempts": failed_attempts,
        "success_urls": success_urls,
    }))


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)

    blacklist_path = str(common.isolate_blacklist())

    coordinator_kwargs = common.frontier_kwargs_from_args(args)
    frontier = common.build_frontier("redis", **coordinator_kwargs)
    if not args.no_clear:
        frontier.clear()

    priority_fn = common.parse_priority_distribution(args.priority_distribution)
    run_id = f"db{int(time.time())}"
    urls = common.make_synthetic_urls(args.urls, args.domains, priority_fn, rng, run_id=run_id)

    insert_start = time.time()
    inserted = sum(1 for url, priority in urls if frontier.add_url(url, priority=priority))
    insert_elapsed = time.time() - insert_start

    redis_conn = frontier.redis_conn
    monitor = common.ResourceMonitor(
        redis_conn=redis_conn, interval=args.monitor_interval, include_children=True
    )

    worker_args = dict(
        duration=args.duration,
        retry_rate=args.retry_rate,
        process_time=args.process_time,
        poll_interval=args.poll_interval,
        max_idle_polls=args.max_idle_polls,
    )

    with tempfile.TemporaryDirectory(prefix="frontier_dist_bench_") as tmp_dir:
        result_paths = [str(Path(tmp_dir) / f"worker_{i}.json") for i in range(args.workers)]

        with monitor:
            run_start = time.time()
            procs = [
                multiprocessing.Process(
                    target=_worker_main,
                    args=(i, coordinator_kwargs, worker_args, result_paths[i], blacklist_path),
                )
                for i in range(args.workers)
            ]
            for proc in procs:
                proc.start()
            join_deadline = run_start + args.duration + 10.0  # grace period beyond worker deadline
            for proc in procs:
                proc.join(timeout=max(0.0, join_deadline - time.time()))
            still_alive = [proc for proc in procs if proc.is_alive()]
            for proc in still_alive:
                proc.terminate()
                proc.join(timeout=5.0)
            run_elapsed = time.time() - run_start

        worker_results = []
        for path in result_paths:
            p = Path(path)
            if p.exists():
                worker_results.append(json.loads(p.read_text()))
            else:
                worker_results.append(None)

    missing = [i for i, r in enumerate(worker_results) if r is None]

    total_claims_issued = sum(r["claims_issued"] for r in worker_results if r)
    total_failed_attempts = sum(r["failed_attempts"] for r in worker_results if r)

    success_owner: dict[str, list[int]] = {}
    for r in worker_results:
        if not r:
            continue
        for url in r["success_urls"]:
            success_owner.setdefault(url, []).append(r["worker_id"])

    duplicate_claims = {url: owners for url, owners in success_owner.items() if len(owners) > 1}
    total_completed = len(success_owner)

    final_status_counts = frontier.get_status_counts()
    frontier.close()

    result = {
        "run": {"tool": "distributed_benchmark", "timestamp": common.now_iso()},
        "config": {
            "workers": args.workers,
            "urls": args.urls,
            "duration_s": args.duration,
            "retry_rate": args.retry_rate,
            "domains": args.domains,
            "priority_distribution": args.priority_distribution,
            "rate_limit": args.rate_limit,
            "lease_ttl": args.lease_ttl,
            "namespace": args.namespace,
            "redis_db": args.redis_db,
        },
        "insert": {
            "attempted": len(urls),
            "inserted": inserted,
            "elapsed_s": insert_elapsed,
            "urls_per_sec": (inserted / insert_elapsed) if insert_elapsed > 0 else None,
        },
        "workers": {
            "requested": args.workers,
            "missing_results": missing,
            "per_worker": worker_results,
        },
        "aggregate": {
            "elapsed_s": run_elapsed,
            "total_claims_issued": total_claims_issued,
            "total_unique_urls_completed": total_completed,
            "duplicate_successful_claims": len(duplicate_claims),
            "duplicate_claim_details": duplicate_claims,
            "total_failed_attempts": total_failed_attempts,
            "claims_per_sec": (total_claims_issued / run_elapsed) if run_elapsed > 0 else None,
            "completions_per_sec": (total_completed / run_elapsed) if run_elapsed > 0 else None,
        },
        "final_status_counts": final_status_counts,
        "resource_usage": monitor.summary(),
        "total_runtime_s": insert_elapsed + run_elapsed,
    }

    common.write_result(result, args.output, args.format)

    if duplicate_claims:
        print(
            f"\nWARNING: {len(duplicate_claims)} URL(s) were successfully completed by more "
            "than one worker process -- this indicates a distributed claim-safety violation.",
            file=sys.stderr,
        )
    if missing:
        print(f"\nWARNING: {len(missing)} worker(s) did not report results (worker ids: {missing}).",
              file=sys.stderr)


if __name__ == "__main__":
    main()
