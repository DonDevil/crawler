#!/usr/bin/env python3
"""Heartbeat endurance test: a synthetic fetch that intentionally runs
longer than `lease_ttl`, run once with the shared heartbeat helper
(`core.claim_heartbeat.run_with_heartbeat`) and once without it (negative
control), against the Redis frontier.

This exercises exactly the mechanism described in
docs/architecture/frontier-step5.md: a background recovery sweep (the same
`reclaim_and_promote` the real asyncio recovery task calls on a timer) runs
concurrently with the "fetch" in both scenarios.

  - Heartbeat ENABLED:  the claim is renewed every `--heartbeat-interval`
    seconds, so it never looks abandoned to the recovery sweep. Expected:
    the URL is never reclaimed, and the original worker's completion at the
    end succeeds normally.
  - Heartbeat DISABLED (negative control): nothing renews the claim. Once
    `lease_ttl` elapses, the recovery sweep reclaims it -- possibly before
    the synthetic fetch even finishes. Expected: the URL gets reclaimed
    (visible in the recovery log and the final status counts), and the
    original worker's completion at the end is silently rejected as stale.

Uses core/claim_heartbeat.py and core/frontier_executor.py exactly as the
real crawler backends do -- no frontier, Lua script, or crawler worker code
is modified or reimplemented here.

Examples:
    python tests/benchmarks/heartbeat_endurance.py
    python tests/benchmarks/heartbeat_endurance.py --work-duration 10 --lease-ttl 3 \\
        --recovery-interval 0.5 --base-backoff 0.2
    python tests/benchmarks/heartbeat_endurance.py --mode enabled
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

from core.claim_heartbeat import ClaimLostError, run_with_heartbeat  # noqa: E402
from core.frontier_executor import AsyncFrontier  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["enabled", "disabled", "both"], default="both",
                   help="Which scenario(s) to run (default: both, for direct comparison)")
    p.add_argument("--work-duration", type=float, default=6.0,
                   help="Synthetic fetch duration in seconds -- should exceed --lease-ttl")
    p.add_argument("--heartbeat-interval", type=float, default=None,
                   help="Renewal cadence; default derives lease_ttl/3 (core.claim_heartbeat)")
    p.add_argument("--recovery-interval", type=float, default=0.3,
                   help="How often the simulated background recovery task sweeps")
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    common.add_common_frontier_args(
        p,
        default_namespace="bench_heartbeat",
        default_lease_ttl=2.0,
        default_base_backoff=0.2,
    )
    return p.parse_args()


async def _recovery_loop(frontier, interval: float, stop_event: asyncio.Event, log: list) -> None:
    """Mirrors core/crawler_manager.py's `_recovery_loop` shape exactly
    (ADR §7) -- calls the same `reclaim_and_promote`, on the same kind of
    timer, just driven by this script instead of CrawlerManager."""
    while not stop_event.is_set():
        try:
            reclaimed, requeued = await frontier.reclaim_and_promote()
            if reclaimed or requeued:
                log.append({"t": time.time(), "reclaimed": reclaimed, "requeued": requeued})
        except Exception as exc:  # pragma: no cover - defensive, mirrors production loop
            log.append({"t": time.time(), "error": str(exc)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _run_scenario(args, heartbeat_enabled: bool) -> dict:
    kwargs = common.frontier_kwargs_from_args(args)
    raw_frontier = common.build_frontier("redis", **kwargs)
    label = "enabled" if heartbeat_enabled else "disabled"
    # Isolate each scenario's URL within the shared namespace/db so running
    # --mode both doesn't let one scenario's state leak into the other.
    raw_frontier.clear()

    async_frontier = AsyncFrontier(raw_frontier)
    url = f"https://bench-heartbeat.example.test/{label}-{int(time.time())}"

    raw_frontier.add_url(url, priority=5)
    claim = raw_frontier.get_next_url()
    assert claim is not None and claim.url == url

    heartbeat_interval = args.heartbeat_interval
    if heartbeat_interval is None:
        from core.claim_heartbeat import default_heartbeat_interval
        heartbeat_interval = default_heartbeat_interval(args.lease_ttl)

    stop_event = asyncio.Event()
    recovery_log: list[dict] = []
    recovery_task = asyncio.create_task(
        _recovery_loop(async_frontier, args.recovery_interval, stop_event, recovery_log)
    )

    t_start = time.time()
    claim_lost = False
    final_claim = claim
    try:
        work_coro = asyncio.sleep(args.work_duration)
        if heartbeat_enabled:
            _, final_claim = await run_with_heartbeat(async_frontier, claim, work_coro, heartbeat_interval)
        else:
            await work_coro
    except ClaimLostError:
        claim_lost = True
    work_elapsed = time.time() - t_start

    stop_event.set()
    await recovery_task

    status_before_completion = raw_frontier.get_status_counts()
    if not claim_lost:
        raw_frontier.mark_visited(final_claim)
    status_after_completion = raw_frontier.get_status_counts()

    completion_took_effect = status_before_completion != status_after_completion
    reclaimed_at_any_point = any(entry.get("reclaimed") for entry in recovery_log)

    final_status = raw_frontier.get_status_counts()
    raw_frontier.close()

    if heartbeat_enabled:
        expected = (not claim_lost) and (not reclaimed_at_any_point) and completion_took_effect
    else:
        expected = reclaimed_at_any_point and (claim_lost or not completion_took_effect)

    return {
        "scenario": f"heartbeat_{label}",
        "url": url,
        "config": {
            "work_duration_s": args.work_duration,
            "lease_ttl_s": args.lease_ttl,
            "heartbeat_interval_s": heartbeat_interval,
            "recovery_interval_s": args.recovery_interval,
        },
        "work_elapsed_s": work_elapsed,
        "claim_lost_mid_work": claim_lost,
        "recovery_sweep_log": recovery_log,
        "reclaimed_at_any_point": reclaimed_at_any_point,
        "final_mark_visited_took_effect": completion_took_effect,
        "final_status_counts": final_status,
        "matches_expected_behavior": expected,
    }


async def _main_async(args) -> dict:
    results = []
    if args.mode in ("enabled", "both"):
        results.append(await _run_scenario(args, heartbeat_enabled=True))
    if args.mode in ("disabled", "both"):
        results.append(await _run_scenario(args, heartbeat_enabled=False))
    return {"scenarios": results}


def main() -> None:
    args = parse_args()
    payload = asyncio.run(_main_async(args))

    result = {"run": {"tool": "heartbeat_endurance", "timestamp": common.now_iso()}, **payload}
    common.write_result(result, args.output, args.format)

    failures = [s["scenario"] for s in payload["scenarios"] if not s["matches_expected_behavior"]]
    if failures:
        print(f"\nWARNING: scenario(s) did not match expected heartbeat behavior: {failures}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
