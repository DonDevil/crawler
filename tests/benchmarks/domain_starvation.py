#!/usr/bin/env python3
"""Deterministic domain-starvation audit tool (Step 8).

Manual investigation script -- deliberately not named `*_test.py`/`test_*.py`
so pytest never auto-collects it (matches `priority_ratelimit.py`'s
convention; see docs/architecture/domain-starvation-audit.md for the
findings this tool produced).

Answers one question per scenario: can a domain with valid queued URLs go
unclaimed indefinitely because the scheduler keeps preferring other domains?
Each scenario claims deterministically (never sleeps to "let the crawler
run" -- either polls a bounded number of times or claims a bounded count)
and reports per-domain fairness metrics, not just "it eventually got a
claim."

Scenarios:
    finite            Scenario 1 -- fixed high/low priority workload, claim to empty.
    rate-limit-skip    Scenario 2 -- rate-gated top domain must not block an eligible one.
    replenish          Scenario 3 -- continuously replenished A vs. fixed B.
    scan-limit-window  Reproduces frontier-optimization-audit.md Sec 4.6/8.3: a
                       domain ranked outside the Redis `domain_scan_limit`
                       window. Redis-only (local frontier has no K bound).
    retries            Scenario in Sec 11 -- repeatedly failing high-priority
                       domain vs. fixed low-priority domain.
    multi-worker       Scenario in Sec 12 -- N concurrent claimers (threads)
                       against one frontier instance.
    recovery           Scenario in Sec 4/Mechanism H -- repeated lease expiry
                       of a high-priority domain vs. a fixed low-priority one.
                       Redis-only (local frontier has no lease expiry).

Examples:
    python tests/benchmarks/domain_starvation.py finite --frontier local
    python tests/benchmarks/domain_starvation.py replenish --frontier redis --num-claims 500
    python tests/benchmarks/domain_starvation.py scan-limit-window --domain-scan-limit 10 --domain-count 15
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

from core.frontier import FrontierClaim  # noqa: E402


# ---------------------------------------------------------------------------
# Fairness metrics -- shared by every scenario below.
# ---------------------------------------------------------------------------

def _new_fairness_row(priority: Optional[int], seeded: Optional[int]) -> dict:
    return {
        "priority": priority,
        "seeded": seeded,
        "claims": 0,
        "first_claim_t": None,
        "last_claim_t": None,
        "max_wait_between_claims_s": 0.0,
        "pct_of_total": 0.0,
        "max_other_domain_streak": 0,
    }


def _longest_run_excluding(claim_log: list[dict], domain: str) -> int:
    """Longest run of consecutive claims made to a single other domain while
    `domain` waited (a coarse but cheap proxy for "how long was this domain
    passed over in favor of one competitor")."""
    longest = run = 0
    run_domain: Optional[str] = None
    for entry in claim_log:
        if entry["domain"] == domain:
            run = 0
            run_domain = None
            continue
        run = run + 1 if entry["domain"] == run_domain else 1
        run_domain = entry["domain"]
        longest = max(longest, run)
    return longest


def compute_fairness(claim_log: list[dict], domain_meta: dict[str, dict]) -> dict[str, dict]:
    """Derive per-domain fairness metrics from a chronological claim log.

    `claim_log` entries need `domain` and `t_offset_s`. `domain_meta` maps
    domain -> {"priority": int, "seeded": int} for domains seeded up front
    (continuously-replenished domains may not have a fixed `seeded` count).
    """
    stats: dict[str, dict] = {
        domain: _new_fairness_row(meta.get("priority"), meta.get("seeded"))
        for domain, meta in domain_meta.items()
    }

    last_seen_t: dict[str, float] = {}
    for entry in claim_log:
        domain, t = entry["domain"], entry["t_offset_s"]
        row = stats.setdefault(domain, _new_fairness_row(entry.get("priority"), None))
        row["claims"] += 1
        row["first_claim_t"] = row["first_claim_t"] if row["first_claim_t"] is not None else t
        if domain in last_seen_t:
            row["max_wait_between_claims_s"] = max(row["max_wait_between_claims_s"], t - last_seen_t[domain])
        row["last_claim_t"] = t
        last_seen_t[domain] = t

    total = len(claim_log)
    for domain, row in stats.items():
        row["pct_of_total"] = (row["claims"] / total * 100.0) if total else 0.0
        row["max_other_domain_streak"] = _longest_run_excluding(claim_log, domain)

    return stats


def _seed_fixed_domain(frontier, domain: str, priority: int, count: int) -> None:
    for i in range(count):
        frontier.add_url(f"https://{domain}/p{i}", priority=priority)


def _claim_to_log_entry(claim: FrontierClaim, t_start: float) -> dict:
    return {
        "t_offset_s": round(time.time() - t_start, 4),
        "url": claim.url,
        "domain": claim.domain,
        "priority": claim.priority,
        "attempt": claim.attempt,
    }


def _run_bounded_claim_loop(
    frontier,
    num_claims: int,
    max_idle_polls: int,
    poll_interval: float,
    on_claim,
) -> list[dict]:
    """Claim up to `num_claims` times, polling a bounded number of times
    when the frontier is momentarily empty. `on_claim(claim)` handles
    completion and any per-claim side effects (e.g. replenishing a domain);
    it returns `True` to stop the loop early. Deterministic: never runs for
    an unbounded amount of wall time or an unbounded number of claims."""
    claim_log: list[dict] = []
    t_start = time.time()
    idle_polls = 0
    while len(claim_log) < num_claims:
        claim = frontier.get_next_url()
        if claim is None:
            idle_polls += 1
            if idle_polls > max_idle_polls:
                break
            time.sleep(poll_interval)
            continue
        idle_polls = 0
        claim_log.append(_claim_to_log_entry(claim, t_start))
        if on_claim(claim):
            break
    return claim_log


# ---------------------------------------------------------------------------
# Scenario 1 -- finite priority ordering
# ---------------------------------------------------------------------------

def scenario_finite(frontier, run_id: str, high_count: int, low_count: int,
                     high_priority: int, low_priority: int) -> dict:
    """Fixed high/low priority workload. Claim to empty; every URL must be
    claimable, and priority order must be respected while both domains have
    eligible work."""
    high_domain = f"high-{run_id}.example.test"
    low_domain = f"low-{run_id}.example.test"
    domain_meta = {
        high_domain: {"priority": high_priority, "seeded": high_count},
        low_domain: {"priority": low_priority, "seeded": low_count},
    }

    _seed_fixed_domain(frontier, high_domain, high_priority, high_count)
    _seed_fixed_domain(frontier, low_domain, low_priority, low_count)

    claim_log = []
    t_start = time.time()
    while True:
        claim = frontier.get_next_url()
        if claim is None:
            break
        claim_log.append(_claim_to_log_entry(claim, t_start))
        frontier.mark_visited(claim)

    fairness = compute_fairness(claim_log, domain_meta)
    both_fully_claimed = (
        fairness[high_domain]["claims"] == high_count
        and fairness[low_domain]["claims"] == low_count
    )
    return {
        "scenario": "finite",
        "domain_meta": domain_meta,
        "claim_log": claim_log,
        "fairness": fairness,
        "all_urls_eventually_claimed": both_fully_claimed,
    }


# ---------------------------------------------------------------------------
# Scenario 2 -- rate-limit skip behavior
# ---------------------------------------------------------------------------

def scenario_rate_limit_skip(frontier, run_id: str) -> dict:
    """A rate-gated top-priority domain must not block a lower-priority but
    currently-eligible domain -- confirms the skip-not-block claim in
    docs/architecture/frontier-adr.md Sec 6."""
    hot = f"hot-{run_id}.example.test"
    cold = f"cold-{run_id}.example.test"

    frontier.add_url(f"https://{hot}/a", priority=1)
    first = frontier.get_next_url()
    frontier.mark_visited(first)

    frontier.add_url(f"https://{hot}/b", priority=1)
    frontier.add_url(f"https://{cold}/a", priority=50)

    second = frontier.get_next_url()
    skipped_not_blocked = second is not None and second.domain == cold

    return {
        "scenario": "rate_limit_skip",
        "first_claim_domain": first.domain,
        "second_claim_domain": second.domain if second else None,
        "skipped_not_blocked": skipped_not_blocked,
    }


# ---------------------------------------------------------------------------
# Scenario 3 -- continuous high-priority replenishment
# ---------------------------------------------------------------------------

def scenario_replenish(frontier, run_id: str, num_claims: int, low_count: int,
                        high_priority: int, low_priority: int,
                        replenish_batch: int, max_idle_polls: int,
                        poll_interval: float) -> dict:
    """A = continuously replenished, always eligible. B = fixed queued work.
    Runs for a fixed number of claims (not fixed wall time) so the scenario
    stays deterministic. Measures whether B is ever claimed."""
    high = f"repl-a-{run_id}.example.test"
    low = f"repl-b-{run_id}.example.test"
    domain_meta = {
        high: {"priority": high_priority, "seeded": None},
        low: {"priority": low_priority, "seeded": low_count},
    }

    seq = 0

    def top_up() -> None:
        nonlocal seq
        for _ in range(replenish_batch):
            frontier.add_url(f"https://{high}/gen{seq}", priority=high_priority)
            seq += 1

    top_up()
    _seed_fixed_domain(frontier, low, low_priority, low_count)

    def on_claim(claim: FrontierClaim) -> bool:
        frontier.mark_visited(claim)
        if claim.domain == high:
            top_up()
        return False

    claim_log = _run_bounded_claim_loop(frontier, num_claims, max_idle_polls, poll_interval, on_claim)

    fairness = compute_fairness(claim_log, domain_meta)
    preview = claim_log if len(claim_log) <= 100 else claim_log[:50] + ["...truncated..."] + claim_log[-50:]
    return {
        "scenario": "replenish",
        "domain_meta": domain_meta,
        "claim_log": preview,
        "total_claims_made": len(claim_log),
        "fairness": fairness,
        "low_priority_domain_starved": fairness.get(low, {}).get("claims", 0) == 0,
    }


# ---------------------------------------------------------------------------
# Scenario -- Redis domain_scan_limit (K) visibility window
# ---------------------------------------------------------------------------

def scenario_scan_limit_window(frontier, run_id: str, domain_count: int,
                                domain_scan_limit: int, num_claims: int,
                                replenish_batch: int, max_idle_polls: int,
                                poll_interval: float) -> dict:
    """Reproduces frontier-optimization-audit.md Sec 4.6/8.3: seed more
    distinct, continuously-replenished domains than `domain_scan_limit`, all
    at better priority than one fixed low-priority "victim" domain. If the
    victim is ranked outside the K window and stays there, it must never be
    claimed -- this is the K-bounded-visibility mechanism, distinct from
    ordinary strict-priority preference."""
    filler_domains = [f"scanfill{i}-{run_id}.example.test" for i in range(domain_count)]
    victim = f"victim-{run_id}.example.test"
    domain_meta = {d: {"priority": 1, "seeded": None} for d in filler_domains}
    domain_meta[victim] = {"priority": 100, "seeded": 5}

    seq = 0

    def top_up() -> None:
        nonlocal seq
        for domain in filler_domains:
            for _ in range(replenish_batch):
                frontier.add_url(f"https://{domain}/gen{seq}", priority=1)
                seq += 1

    top_up()
    _seed_fixed_domain(frontier, victim, 100, 5)

    def on_claim(claim: FrontierClaim) -> bool:
        frontier.mark_visited(claim)
        if claim.domain != victim:
            top_up()
        return False

    claim_log = _run_bounded_claim_loop(frontier, num_claims, max_idle_polls, poll_interval, on_claim)

    fairness = compute_fairness(claim_log, domain_meta)
    return {
        "scenario": "scan_limit_window",
        "domain_scan_limit": domain_scan_limit,
        "domain_count_seeded": domain_count,
        "domain_meta": domain_meta,
        "total_claims_made": len(claim_log),
        "victim_claims": fairness.get(victim, {}).get("claims", 0),
        "victim_starved": fairness.get(victim, {}).get("claims", 0) == 0,
        "fairness_summary": {d: {"claims": s["claims"]} for d, s in fairness.items()},
    }


# ---------------------------------------------------------------------------
# Scenario -- retries
# ---------------------------------------------------------------------------

def scenario_retries(frontier, run_id: str, high_priority: int, low_priority: int,
                      low_count: int, num_claims: int, max_idle_polls: int,
                      poll_interval: float) -> dict:
    """A high-priority domain whose URLs always fail (and get requeued with
    backoff) vs. a fixed low-priority domain. Determines whether repeated
    retry/requeue cycles of A can crowd out B."""
    high = f"retry-a-{run_id}.example.test"
    low = f"retry-b-{run_id}.example.test"
    domain_meta = {
        high: {"priority": high_priority, "seeded": 3},
        low: {"priority": low_priority, "seeded": low_count},
    }

    _seed_fixed_domain(frontier, high, high_priority, 3)
    _seed_fixed_domain(frontier, low, low_priority, low_count)

    def on_claim(claim: FrontierClaim) -> bool:
        if claim.domain == high:
            frontier.mark_failed(claim, "synthetic failure")
        else:
            frontier.mark_visited(claim)
        return not frontier.has_pending()

    claim_log = _run_bounded_claim_loop(frontier, num_claims, max_idle_polls, poll_interval, on_claim)

    fairness = compute_fairness(claim_log, domain_meta)
    return {
        "scenario": "retries",
        "domain_meta": domain_meta,
        "total_claims_made": len(claim_log),
        "fairness": fairness,
        "low_priority_domain_starved": fairness.get(low, {}).get("claims", 0) == 0,
    }


# ---------------------------------------------------------------------------
# Scenario -- multi-worker concurrent claiming
# ---------------------------------------------------------------------------

def _claim_loop_thread(frontier, claim_log: list[dict], log_lock: threading.Lock, t_start: float) -> None:
    while True:
        claim = frontier.get_next_url()
        if claim is None:
            if not frontier.has_pending():
                return
            time.sleep(0.02)
            continue
        with log_lock:
            claim_log.append(_claim_to_log_entry(claim, t_start))
        frontier.mark_visited(claim)


def _run_multi_worker_once(frontier, workers: int, high_count: int, low_count: int,
                            high: str, low: str, domain_meta: dict) -> dict:
    _seed_fixed_domain(frontier, high, domain_meta[high]["priority"], high_count)
    _seed_fixed_domain(frontier, low, domain_meta[low]["priority"], low_count)

    claim_log: list[dict] = []
    log_lock = threading.Lock()
    t_start = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_claim_loop_thread, frontier, claim_log, log_lock, t_start) for _ in range(workers)]
        for f in futures:
            f.result(timeout=60)

    claim_log.sort(key=lambda e: e["t_offset_s"])
    urls_seen = [e["url"] for e in claim_log]
    fairness = compute_fairness(claim_log, domain_meta)
    return {
        "total_claims": len(claim_log),
        "duplicate_claims": len(urls_seen) - len(set(urls_seen)),
        "fairness": fairness,
        "both_domains_fully_drained": (
            fairness[high]["claims"] == high_count and fairness[low]["claims"] == low_count
        ),
    }


def scenario_multi_worker(frontier, run_id: str, worker_counts: list[int],
                           high_count: int, low_count: int,
                           high_priority: int, low_priority: int) -> dict:
    """Repeats Scenario 1 with N threads concurrently calling `get_next_url`
    against the same frontier instance, to check whether concurrent claiming
    preserves priority/fairness semantics (a distinct question from
    duplicate-claim safety, which is already covered by
    tests/redis_frontier_test.py)."""
    results = {}
    for workers in worker_counts:
        high = f"mw{workers}-a-{run_id}.example.test"
        low = f"mw{workers}-b-{run_id}.example.test"
        domain_meta = {
            high: {"priority": high_priority, "seeded": high_count},
            low: {"priority": low_priority, "seeded": low_count},
        }
        results[workers] = _run_multi_worker_once(frontier, workers, high_count, low_count, high, low, domain_meta)
    return {"scenario": "multi_worker", "by_worker_count": results}


# ---------------------------------------------------------------------------
# Scenario -- recovery/reclaim (Redis only)
# ---------------------------------------------------------------------------

def _recovery_cycles(frontier, low: str, cycles: int, claim_log: list[dict], t_start: float) -> list[dict]:
    """Repeatedly claim (leaving high-priority claims to expire, simulating
    a crashed worker) and sweep `reclaim_and_promote` after each lease
    window elapses. Low-priority claims are completed immediately."""
    reclaim_events = []
    for cycle in range(cycles):
        claim = frontier.get_next_url()
        if claim is not None:
            claim_log.append(_claim_to_log_entry(claim, t_start))
            if claim.domain == low:
                frontier.mark_visited(claim)
        time.sleep(frontier.lease_ttl + 0.2)
        reclaimed, requeued = frontier.reclaim_and_promote()
        reclaim_events.append({"cycle": cycle, "reclaimed": reclaimed, "requeued": requeued})
    return reclaim_events


def _drain_remaining(frontier, claim_log: list[dict], t_start: float) -> None:
    while True:
        claim = frontier.get_next_url()
        if claim is None:
            return
        claim_log.append(_claim_to_log_entry(claim, t_start))
        frontier.mark_visited(claim)


def scenario_recovery(frontier, run_id: str, low_priority: int, low_count: int,
                       high_priority: int, cycles: int) -> dict:
    """A high-priority domain is claimed but never completed (simulating a
    crashed worker); its lease expires and `reclaim_and_promote` requeues it,
    repeated `cycles` times, interleaved with attempts to claim the
    low-priority domain. Determines whether repeated reclaim of A can starve
    B. Requires a frontier with a short `lease_ttl`."""
    high = f"rec-a-{run_id}.example.test"
    low = f"rec-b-{run_id}.example.test"
    domain_meta = {
        high: {"priority": high_priority, "seeded": 1},
        low: {"priority": low_priority, "seeded": low_count},
    }

    frontier.add_url(f"https://{high}/p0", priority=high_priority)
    _seed_fixed_domain(frontier, low, low_priority, low_count)

    claim_log: list[dict] = []
    t_start = time.time()
    reclaim_events = _recovery_cycles(frontier, low, cycles, claim_log, t_start)
    _drain_remaining(frontier, claim_log, t_start)

    fairness = compute_fairness(claim_log, domain_meta)
    return {
        "scenario": "recovery",
        "domain_meta": domain_meta,
        "reclaim_events": reclaim_events,
        "fairness": fairness,
        "low_priority_domain_starved": fairness.get(low, {}).get("claims", 0) == 0,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", choices=[
        "finite", "rate-limit-skip", "replenish", "scan-limit-window",
        "retries", "multi-worker", "recovery",
    ])
    p.add_argument("--frontier", choices=["local", "redis"], default="local")
    p.add_argument("--high-priority", type=int, default=1)
    p.add_argument("--low-priority", type=int, default=10)
    p.add_argument("--high-count", type=int, default=100)
    p.add_argument("--low-count", type=int, default=10)
    p.add_argument("--num-claims", type=int, default=300)
    p.add_argument("--replenish-batch", type=int, default=5)
    p.add_argument("--domain-count", type=int, default=15)
    p.add_argument("--domain-scan-limit", type=int, default=10)
    p.add_argument("--worker-counts", default="1,2,4,8")
    p.add_argument("--recovery-cycles", type=int, default=3)
    p.add_argument("--max-idle-polls", type=int, default=200)
    p.add_argument("--poll-interval", type=float, default=0.02)
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    common.add_common_frontier_args(p, default_namespace="bench_starvation", default_rate_limit=0.0)
    return p.parse_args()


def build(args: argparse.Namespace):
    common.isolate_blacklist()
    kwargs = common.frontier_kwargs_from_args(args)
    if args.scenario == "scan-limit-window":
        kwargs["domain_scan_limit"] = args.domain_scan_limit
    frontier = common.build_frontier(args.frontier, **kwargs)
    frontier.clear()
    return frontier


def dispatch(args: argparse.Namespace, frontier, run_id: str) -> dict:
    if args.scenario == "finite":
        return scenario_finite(frontier, run_id, args.high_count, args.low_count,
                                args.high_priority, args.low_priority)
    if args.scenario == "rate-limit-skip":
        return scenario_rate_limit_skip(frontier, run_id)
    if args.scenario == "replenish":
        return scenario_replenish(frontier, run_id, args.num_claims, args.low_count,
                                   args.high_priority, args.low_priority,
                                   args.replenish_batch, args.max_idle_polls, args.poll_interval)
    if args.scenario == "scan-limit-window":
        return scenario_scan_limit_window(frontier, run_id, args.domain_count,
                                           args.domain_scan_limit, args.num_claims,
                                           args.replenish_batch, args.max_idle_polls, args.poll_interval)
    if args.scenario == "retries":
        return scenario_retries(frontier, run_id, args.high_priority, args.low_priority,
                                 args.low_count, args.num_claims, args.max_idle_polls, args.poll_interval)
    if args.scenario == "multi-worker":
        worker_counts = [int(x) for x in args.worker_counts.split(",")]
        return scenario_multi_worker(frontier, run_id, worker_counts, args.high_count,
                                      args.low_count, args.high_priority, args.low_priority)
    if args.scenario == "recovery":
        return scenario_recovery(frontier, run_id, args.low_priority, args.low_count,
                                  args.high_priority, args.recovery_cycles)
    raise ValueError(f"unhandled scenario {args.scenario!r}")


def main() -> None:
    args = parse_args()
    if args.scenario in {"scan-limit-window", "recovery"} and args.frontier != "redis":
        print(f"Scenario {args.scenario!r} is Redis-only (see docstring); forcing --frontier redis", file=sys.stderr)
        args.frontier = "redis"

    frontier = build(args)
    run_id = f"ds{int(time.time())}"

    try:
        result = dispatch(args, frontier, run_id)
    finally:
        frontier.clear()
        frontier.close()

    output = {
        "run": {"tool": "domain_starvation", "timestamp": common.now_iso(), "frontier": args.frontier},
        "args": vars(args),
        "result": result,
    }
    common.write_result(output, args.output, args.format)


if __name__ == "__main__":
    main()
