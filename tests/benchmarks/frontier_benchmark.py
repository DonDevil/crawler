#!/usr/bin/env python3
"""Frontier throughput benchmark (local/SQLite vs Redis), single process.

Measures insert throughput, claim throughput, completion throughput, checks
for duplicate successful claims, and reports three distinct latencies --
queue_wait (insertion -> successful claim, i.e. how long a URL sat waiting),
claim_operation (the get_next_url() call itself), and completion_operation
(the mark_visited() call itself) -- all against a synthetic in-memory
workload (no real HTTP fetching, per Step 6's scope: this validates the
frontier, not the crawler).

Pass --no-rate-limit to disable per-domain rate limiting (rate_limit=0.0)
for a pure frontier-throughput run that isn't capped by domain pacing.

Workers are real OS threads calling the frontier's synchronous methods
directly (the same execution boundary `AsyncFrontier` uses for Redis via
`asyncio.to_thread` -- see core/frontier_executor.py), so contention between
concurrent callers is real, not simulated.

Examples:
    python tests/benchmarks/frontier_benchmark.py --frontier local --urls 10000 --workers 4
    python tests/benchmarks/frontier_benchmark.py --frontier redis --urls 10000 --workers 4
    python tests/benchmarks/frontier_benchmark.py --frontier redis --urls 5000 --workers 8 \\
        --domains 25 --priority-distribution weighted:1:0.1,5:0.3,10:0.6 --retry-rate 0.15 \\
        --output /tmp/bench.json
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frontier", choices=["local", "redis"], required=True)
    p.add_argument("--urls", type=int, default=2000, help="Number of synthetic URLs to insert")
    p.add_argument("--workers", type=int, default=4, help="Number of concurrent claim/complete threads")
    p.add_argument("--duration", type=float, default=60.0,
                   help="Max seconds for the claim/complete phase, even if not fully drained")
    p.add_argument("--retry-rate", type=float, default=0.0,
                   help="Probability [0,1] that a claimed URL's simulated fetch fails")
    p.add_argument("--domains", type=int, default=10, help="Number of distinct synthetic domains")
    p.add_argument("--priority-distribution", default="fixed:10",
                   help="fixed:<p> | uniform:<lo>-<hi> | weighted:<p1>:<w1>,<p2>:<w2>,...")
    p.add_argument("--process-time", type=float, default=0.0,
                   help="Simulated per-claim processing time in seconds (sleep before completing)")
    p.add_argument("--process-time-jitter", type=float, default=0.0,
                   help="+/- jitter applied to --process-time")
    p.add_argument("--no-rate-limit", action="store_true",
                   help="Disable per-domain rate limiting (overrides --rate-limit with 0.0) "
                        "for a pure frontier-throughput run, unconstrained by domain pacing")
    p.add_argument("--poll-interval", type=float, default=0.01,
                   help="Sleep between get_next_url() polls when the frontier has pending work "
                        "but nothing eligible right now (rate-gated)")
    p.add_argument("--max-idle-polls", type=int, default=500,
                   help="Give up (assume drained) after this many consecutive empty polls")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default=None, help="Optional path to also write results to")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--monitor-interval", type=float, default=0.5)
    p.add_argument("--no-clear", action="store_true",
                   help="Don't clear the namespace before running (default: clear)")
    common.add_common_frontier_args(p)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.no_rate_limit:
        args.rate_limit = 0.0
    rng = random.Random(args.seed)

    common.isolate_blacklist()

    frontier = common.build_frontier(args.frontier, **common.frontier_kwargs_from_args(args))
    if not args.no_clear:
        frontier.clear()

    priority_fn = common.parse_priority_distribution(args.priority_distribution)
    run_id = f"fb{int(time.time())}"
    urls = common.make_synthetic_urls(args.urls, args.domains, priority_fn, rng, run_id=run_id)

    # --- Phase 1: insert -------------------------------------------------
    insert_start = time.time()
    inserted = 0
    insert_times: dict[str, float] = {}
    for url, priority in urls:
        t0 = time.time()
        if frontier.add_url(url, priority=priority):
            inserted += 1
        insert_times[url] = t0
    insert_elapsed = time.time() - insert_start

    # --- Phase 2: claim / process / complete ------------------------------
    #
    # Three distinct latencies are tracked, since they measure very
    # different things and collapsing them is misleading (see README):
    #   - queue_wait: URL insertion timestamp -> successful claim timestamp.
    #     Dominated by how long a URL sat in the queue behind other work /
    #     rate gates -- not the frontier's per-call cost.
    #   - claim_operation: wall-clock time of the get_next_url() call itself
    #     (immediately before -> immediately after it returns).
    #   - completion_operation: wall-clock time of the mark_visited() call
    #     itself (immediately before -> immediately after it returns).
    lock = threading.Lock()
    queue_wait_latencies: list[float] = []
    claim_operation_latencies: list[float] = []
    completion_operation_latencies: list[float] = []
    success_counter: Counter[str] = Counter()
    claims_issued = 0
    failed_attempts = 0
    unique_urls_claimed: set[str] = set()
    stop_event = threading.Event()

    def worker() -> None:
        nonlocal claims_issued, failed_attempts
        idle_polls = 0
        while not stop_event.is_set():
            op_start = time.time()
            claim = frontier.get_next_url()
            op_end = time.time()
            if claim is None:
                if not frontier.has_pending():
                    return
                idle_polls += 1
                if idle_polls > args.max_idle_polls:
                    return
                time.sleep(args.poll_interval)
                continue
            idle_polls = 0

            claim_time = op_end
            with lock:
                claims_issued += 1
                unique_urls_claimed.add(claim.url)
                claim_operation_latencies.append(op_end - op_start)
                t0 = insert_times.get(claim.url)
                if t0 is not None:
                    queue_wait_latencies.append(claim_time - t0)

            if args.process_time > 0 or args.process_time_jitter > 0:
                jitter = random.uniform(-args.process_time_jitter, args.process_time_jitter)
                time.sleep(max(0.0, args.process_time + jitter))

            if random.random() < args.retry_rate:
                frontier.mark_failed(claim, "synthetic-benchmark-failure")
                with lock:
                    failed_attempts += 1
            else:
                comp_start = time.time()
                frontier.mark_visited(claim)
                comp_end = time.time()
                with lock:
                    success_counter[claim.url] += 1
                    completion_operation_latencies.append(comp_end - comp_start)

    redis_conn = getattr(frontier, "redis_conn", None)
    monitor = common.ResourceMonitor(redis_conn=redis_conn, interval=args.monitor_interval)

    with monitor:
        run_start = time.time()
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
        for t in threads:
            t.start()
        deadline = run_start + args.duration
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.time()))
        stop_event.set()
        for t in threads:
            t.join(timeout=5.0)
        run_elapsed = time.time() - run_start

    duplicate_successful_claims = sum(1 for c in success_counter.values() if c > 1)
    total_successes = sum(success_counter.values())
    status_counts = frontier.get_status_counts()
    frontier.close()

    result = {
        "run": {"tool": "frontier_benchmark", "timestamp": common.now_iso(), "frontier": args.frontier},
        "config": {
            "urls": args.urls,
            "workers": args.workers,
            "duration_s": args.duration,
            "retry_rate": args.retry_rate,
            "domains": args.domains,
            "priority_distribution": args.priority_distribution,
            "rate_limit": args.rate_limit,
            "no_rate_limit": args.no_rate_limit,
            "lease_ttl": args.lease_ttl,
            "max_retries": args.max_retries,
            "process_time_s": args.process_time,
            "namespace": args.namespace if args.frontier == "redis" else None,
            "redis_db": args.redis_db if args.frontier == "redis" else None,
        },
        "insert": {
            "attempted": len(urls),
            "inserted": inserted,
            "elapsed_s": insert_elapsed,
            "urls_per_sec": (inserted / insert_elapsed) if insert_elapsed > 0 else None,
        },
        "claim_and_complete": {
            "elapsed_s": run_elapsed,
            "claims_issued": claims_issued,
            "unique_urls_claimed": len(unique_urls_claimed),
            "successful_completions": total_successes,
            "duplicate_successful_claims": duplicate_successful_claims,
            "failed_attempts": failed_attempts,
            "claims_per_sec": (claims_issued / run_elapsed) if run_elapsed > 0 else None,
            "completions_per_sec": (total_successes / run_elapsed) if run_elapsed > 0 else None,
        },
        "queue_wait_latency": common.latency_stats(queue_wait_latencies),
        "claim_operation_latency": common.latency_stats(claim_operation_latencies),
        "completion_operation_latency": common.latency_stats(completion_operation_latencies),
        "final_status_counts": status_counts,
        "resource_usage": monitor.summary(),
        "total_runtime_s": insert_elapsed + run_elapsed,
    }

    common.write_result(result, args.output, args.format)

    if duplicate_successful_claims:
        print(
            f"\nWARNING: {duplicate_successful_claims} URL(s) were successfully completed "
            "more than once -- this indicates a frontier claim-safety violation.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
