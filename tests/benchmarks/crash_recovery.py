#!/usr/bin/env python3
"""Deterministic crash/recovery scenario against the Redis frontier.

Timeline:
  1. Worker A calls get_next_url() and claims a URL.
  2. Worker A is never completed again -- no mark_visited/mark_failed/
     renew_claim call follows, simulating `kill -9` mid-fetch.
  3. We wait past `--lease-ttl`, so the claim is now eligible for reclaim.
  4. We invoke `reclaim_and_promote()` directly -- this is exactly what the
     asyncio background recovery task in core/crawler_manager.py calls on a
     timer in production (docs/architecture/frontier-adr.md §7); calling it
     directly here makes the test deterministic instead of waiting out a
     real scheduler interval.
  5. Once the retry backoff elapses, a second `reclaim_and_promote()` call
     promotes the URL back into its domain queue.
  6. Worker B calls get_next_url() and claims the same URL (new token,
     attempt=2).
  7. Worker A's stale claim is proven inert: its mark_visited() is a
     silent no-op (frontier's token-CAS rejects it).
  8. Worker B completes the URL normally.

Also runs a `--frontier local` variant that demonstrates the *documented
absence* of crash recovery in-process (docs/architecture/frontier-adr.md
§10): a crashed local worker's claim is never reclaimed, by design --
there is no lease-expiry branch for the in-memory frontier, since a
process crash there takes all frontier state down with it.

Examples:
    python tests/benchmarks/crash_recovery.py
    python tests/benchmarks/crash_recovery.py --lease-ttl 5 --base-backoff 1
    python tests/benchmarks/crash_recovery.py --frontier local
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frontier", choices=["local", "redis"], default="redis")
    p.add_argument("--lease-margin", type=float, default=0.5,
                   help="Extra seconds to wait past lease_ttl before reclaiming, to avoid flakiness")
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    common.add_common_frontier_args(
        p,
        default_namespace="bench_crash",
        default_lease_ttl=2.0,
        default_base_backoff=0.5,
    )
    return p.parse_args()


def _timeline_entry(timeline: list, t_start: float, label: str, **extra) -> None:
    timeline.append({"t_offset_s": round(time.time() - t_start, 3), "event": label, **extra})


def run_redis_scenario(args) -> dict:
    kwargs = common.frontier_kwargs_from_args(args)
    kwargs["lease_ttl"] = args.lease_ttl
    kwargs["base_backoff"] = args.base_backoff
    kwargs["max_retries"] = args.max_retries
    frontier = common.build_frontier("redis", **kwargs)
    frontier.clear()

    timeline: list[dict] = []
    t_start = time.time()
    url = f"https://bench-crash.example.test/url-{int(t_start)}"

    frontier.add_url(url, priority=5)
    _timeline_entry(timeline, t_start, "add_url", url=url)

    claim_a = frontier.get_next_url()
    assert claim_a is not None and claim_a.url == url
    _timeline_entry(timeline, t_start, "worker_a_claimed", token=claim_a.token, attempt=claim_a.attempt)

    _timeline_entry(timeline, t_start, "worker_a_killed (no completion, no renewal)")

    wait_s = args.lease_ttl + args.lease_margin
    time.sleep(wait_s)
    _timeline_entry(timeline, t_start, "lease_expired", waited_s=round(wait_s, 3))

    reclaimed, requeued = frontier.reclaim_and_promote()
    _timeline_entry(timeline, t_start, "recovery_sweep_1 (reclaim expired inflight -> retry_scheduled)",
                     reclaimed=reclaimed, requeued=requeued)

    time.sleep(args.base_backoff + 0.2)
    reclaimed2, requeued2 = frontier.reclaim_and_promote()
    _timeline_entry(timeline, t_start, "recovery_sweep_2 (promote due retry_scheduled -> domain queue)",
                     reclaimed=reclaimed2, requeued=requeued2)

    claim_b = frontier.get_next_url()
    assert claim_b is not None and claim_b.url == url, "worker B failed to reclaim the crashed URL"
    _timeline_entry(timeline, t_start, "worker_b_claimed", token=claim_b.token, attempt=claim_b.attempt)

    status_before_stale = frontier.get_status_counts()
    frontier.mark_visited(claim_a)  # worker A's stale claim -- must be a silent no-op
    status_after_stale = frontier.get_status_counts()
    stale_rejected = status_before_stale == status_after_stale
    _timeline_entry(timeline, t_start, "worker_a_stale_mark_visited_attempted", rejected=stale_rejected)

    frontier.mark_visited(claim_b)
    _timeline_entry(timeline, t_start, "worker_b_completed")

    final_status = frontier.get_status_counts()
    frontier.close()

    return {
        "frontier": "redis",
        "url": url,
        "timeline": timeline,
        "worker_a_claim": {"token": claim_a.token, "attempt": claim_a.attempt},
        "worker_b_claim": {"token": claim_b.token, "attempt": claim_b.attempt},
        "stale_claim_correctly_rejected": stale_rejected,
        "recovery_reclaimed_count": reclaimed,
        "recovery_requeued_count": requeued2,
        "final_status_counts": final_status,
        "outcome": "PASS" if (stale_rejected and final_status["visited"] == 1) else "FAIL",
    }


def run_local_scenario(args) -> dict:
    kwargs = common.frontier_kwargs_from_args(args)
    kwargs["lease_ttl"] = args.lease_ttl
    kwargs["base_backoff"] = args.base_backoff
    kwargs["max_retries"] = args.max_retries
    frontier = common.build_frontier("local", **kwargs)

    timeline: list[dict] = []
    t_start = time.time()
    url = f"https://bench-crash.example.test/url-{int(t_start)}"

    frontier.add_url(url, priority=5)
    _timeline_entry(timeline, t_start, "add_url", url=url)

    claim_a = frontier.get_next_url()
    assert claim_a is not None and claim_a.url == url
    _timeline_entry(timeline, t_start, "worker_a_claimed", token=claim_a.token, attempt=claim_a.attempt)

    _timeline_entry(timeline, t_start, "worker_a_killed (no completion, no renewal)")

    time.sleep(args.lease_ttl + args.lease_margin)
    _timeline_entry(timeline, t_start,
                     "lease_would_have_expired -- but the local frontier has no lease-expiry "
                     "reclaim path in-process (ADR §10): a crashed local worker's claim is "
                     "never recovered, since a process crash takes all in-memory state with it")

    claim_b = frontier.get_next_url()
    _timeline_entry(timeline, t_start, "worker_b_get_next_url", got_claim=claim_b is not None)

    final_status = frontier.get_status_counts()
    frontier.close()

    return {
        "frontier": "local",
        "url": url,
        "timeline": timeline,
        "worker_a_claim": {"token": claim_a.token, "attempt": claim_a.attempt},
        "worker_b_claim": None if claim_b is None else {"token": claim_b.token, "attempt": claim_b.attempt},
        "final_status_counts": final_status,
        "outcome": "EXPECTED (no recovery mechanism exists for the local frontier by design)",
        "note": "This is not a bug: docs/architecture/frontier-adr.md §10 explicitly scopes "
                "crash recovery to the Redis backend only. Use --frontier redis to see recovery.",
    }


def main() -> None:
    args = parse_args()
    if args.frontier == "redis":
        result = run_redis_scenario(args)
    else:
        result = run_local_scenario(args)

    result = {"run": {"tool": "crash_recovery", "timestamp": common.now_iso()}, **result}
    common.write_result(result, args.output, args.format)

    if result.get("outcome") == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
