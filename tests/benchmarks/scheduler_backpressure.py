#!/usr/bin/env python3
"""Phase N6 scheduler-vs-worker-rate backpressure reproduction/design harness.

Built to directly test N5's Finding N5-1 (docs/architecture/redis-frontier-
concurrency-audit.md §7/§14): every `crawler/*_crawler.py` backend feeds
Redis claims into an unbounded, un-throttled local `asyncio.Queue`, and the
claim's Redis lease clock starts at claim time (inside `claim_next`'s Lua
script), not at processing-start time (when a worker actually dequeues it
and begins fetching -- `run_with_heartbeat` only starts renewing once that
happens). This script reproduces that mechanism against a **real Redis
frontier** (`core/redis_frontier.py`, unmodified) with a **synthetic**
scheduler/worker loop that mirrors `crawler/hybrid_crawler.py`'s
`scheduler()`/`worker()` shape exactly, parameterized so the local queue can
be unbounded (today's production behavior) or bounded/gated (the candidate
fixes N6 is evaluating).

No frontier code, Lua script, or crawler worker code is modified,
monkeypatched, or reimplemented with different semantics here -- this script
only calls the existing public `Frontier`/`AsyncFrontier`/`claim_heartbeat`
APIs (`core.redis_frontier.RedisURLFrontier`, `core.frontier_executor.
AsyncFrontier`, `core.claim_heartbeat.run_with_heartbeat`) against a
synthetic workload, exactly like every other script in this directory (see
tests/benchmarks/README.md). `_complete()`/`renew_claim()` are read
directly instead of through the `mark_visited`/`mark_failed` wrappers only
so the harness can observe the 'stale' vs 'visited' return value those
wrappers otherwise discard -- this is the same private method the existing
`mark_visited`/`mark_failed` already call, not new behavior.

Fetches are entirely synthetic (`asyncio.sleep`) -- no real HTTP requests,
no real websites, per the N6 phase brief's "no long/uncontrolled crawl"
requirement. All Redis state lives under an explicitly isolated namespace
(default `n6_backpressure_<epoch_ms>`) on **db 2** by default -- the same
db this directory's other benchmark scripts already use, chosen specifically
because it is distinct from both production (db 0, namespace `crawler`) and
the pytest Redis suite (db 1, namespace `test_crawler`), so a benchmark run
here can never collide with either. Passing `--redis-db 0` is refused
outright as a hard safety guard.

Modes:
    overload    Full scheduler+worker+recovery simulation under a
                configurable workload -- the main reproduction/comparison
                tool. Reports the full N6 metric set (see
                docs/architecture/scheduler-backpressure-design.md).
    stale       Deterministic single-URL walkthrough of the exact 7-step
                stale-completion scenario from the N6 brief (claim -> sits
                in local queue -> lease expires -> reclaimed -> promoted ->
                original "worker" finally runs its (instant) simulated
                fetch -> completes with the old token -> verify 'stale' and
                that the newer claim's state is untouched). No scheduler/
                worker tasks involved -- fully deterministic, like
                tests/benchmarks/crash_recovery.py.

Examples:
    python tests/benchmarks/scheduler_backpressure.py overload \\
        --label baseline_unbounded --concurrency 25 --urls 900 --domains 60 \\
        --worker-latency 2.0 --lease-ttl 4.0 --recovery-interval 1.0 \\
        --rate-limit 0.02 --queue-maxsize 0 --output /tmp/n6_baseline.json

    python tests/benchmarks/scheduler_backpressure.py overload \\
        --label option_a_bounded --queue-maxsize 25 ... (same workload flags)

    python tests/benchmarks/scheduler_backpressure.py stale --lease-ttl 2.0
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

from core.claim_heartbeat import (  # noqa: E402
    ClaimLostError,
    resolve_heartbeat_interval,
    run_with_heartbeat,
)
from core.frontier import FrontierUnavailable  # noqa: E402
from core.frontier_executor import AsyncFrontier  # noqa: E402


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    overload = sub.add_parser("overload", help="Full scheduler/worker/recovery simulation")
    overload.add_argument("--label", default="run", help="Free-text tag stored in the output JSON")
    overload.add_argument("--concurrency", type=int, default=25, help="Number of synthetic worker tasks")
    overload.add_argument("--urls", type=int, default=900, help="Number of synthetic URLs to seed")
    overload.add_argument("--domains", type=int, default=60, help="Number of distinct synthetic domains")
    overload.add_argument("--worker-latency", type=float, default=2.0,
                           help="Base synthetic processing duration per claim, seconds")
    overload.add_argument("--worker-latency-jitter", type=float, default=0.2,
                           help="Fractional +/- jitter applied to --worker-latency")
    overload.add_argument("--real-failure-rate", type=float, default=0.0,
                           help="Probability a synthetic fetch is a genuine failure (default 0: every "
                                "actually-processed claim succeeds, so any failed_permanent outcome "
                                "in the results is unambiguously reclaim-driven, not a real fetch "
                                "failure -- see N6 brief §10/F)")
    overload.add_argument("--recovery-interval", type=float, default=1.0,
                           help="Seconds between reclaim_and_promote sweeps (mirrors "
                                "core/crawler_manager.py's _recovery_loop)")
    overload.add_argument("--queue-maxsize", type=int, default=0,
                           help="Local asyncio.Queue maxsize; 0 = unbounded (today's production "
                                "behavior). N = Option A (N=concurrency) / Option B (N=k*concurrency)")
    overload.add_argument("--slot-gate", action="store_true",
                           help="Option C/D: scheduler only claims when a worker slot is free -- "
                                "acquires a concurrency-sized semaphore before each claim, held for "
                                "the claim's full lifetime (claim -> completion), released by the "
                                "worker only after mark_*/abandon, not merely after dequeue")
    overload.add_argument("--idle-poll-interval", type=float, default=0.1,
                           help="Scheduler's sleep when a claim attempt returns None (compressed vs. "
                                "production's 0.5s to match this harness's compressed time constants)")
    overload.add_argument("--idle-loops-to-stop", type=int, default=5,
                           help="Consecutive idle+drained loops before the run is considered finished "
                                "(mirrors crawler/hybrid_crawler.py's idle_loops>=10 shutdown check)")
    overload.add_argument("--max-wall-time", type=float, default=180.0,
                           help="Hard safety cap on total run duration, seconds")
    overload.add_argument("--sample-interval", type=float, default=0.25,
                           help="Local-queue-depth / Redis-inflight sampling cadence, seconds")
    overload.add_argument("--seed", type=int, default=1234, help="RNG seed for jitter/failure draws")
    overload.add_argument("--output", default=None)
    overload.add_argument("--format", choices=["json", "csv"], default="json")
    overload.add_argument("--dump-lifecycle", default=None,
                           help="Optional path to also dump every per-token lifecycle record as JSON "
                                "(large; omit for normal runs)")
    overload.add_argument("--reclaim-batch-size", type=int, default=200,
                           help="Batch size passed to reclaim_and_promote each sweep")
    common.add_common_frontier_args(
        overload,
        default_namespace=None,  # set per-run below (needs a timestamp)
        default_rate_limit=0.02,
        default_max_retries=3,
        default_base_backoff=0.5,
        default_max_backoff=3.0,
        default_lease_ttl=4.0,
    )

    stale = sub.add_parser("stale", help="Deterministic single-URL stale-completion scenario")
    stale.add_argument("--lease-margin", type=float, default=0.5,
                        help="Extra seconds waited past lease_ttl before triggering the recovery sweep")
    stale.add_argument("--output", default=None)
    stale.add_argument("--format", choices=["json", "csv"], default="json")
    common.add_common_frontier_args(
        stale,
        default_namespace=None,
        default_lease_ttl=2.0,
        default_base_backoff=0.3,
    )

    return p.parse_args()


def _namespace_for(args, prefix: str) -> str:
    if getattr(args, "namespace", None):
        return args.namespace
    return f"{prefix}_{int(time.time() * 1000)}"


# ---------------------------------------------------------------------------
# Instrumented AsyncFrontier -- counts heartbeat renewals per token without
# touching core/claim_heartbeat.py or core/frontier_executor.py. Subclassing
# and overriding one method, same idiom as this repo's own adapter pattern.
# ---------------------------------------------------------------------------

class _CountingAsyncFrontier(AsyncFrontier):
    def __init__(self, frontier, renewal_counts: dict):
        super().__init__(frontier)
        self._renewal_counts = renewal_counts

    async def renew_claim(self, claim):
        self._renewal_counts[claim.token] = self._renewal_counts.get(claim.token, 0) + 1
        return await super().renew_claim(claim)


# ---------------------------------------------------------------------------
# overload mode
# ---------------------------------------------------------------------------

async def _complete_and_record(raw_frontier, claim, outcome: str, error: str, records: dict) -> str:
    """Call the same private `_complete` that `mark_visited`/`mark_failed`
    call internally, off the event loop thread (matching AsyncFrontier's own
    offload policy), so the harness can observe 'stale' vs the real outcome
    -- the only reason this isn't just `async_frontier.mark_visited(claim)`.
    """
    result = await asyncio.to_thread(raw_frontier._complete, claim, outcome, error)
    rec = records[claim.token]
    rec["completed_at"] = time.time()
    rec["completion_result"] = result
    return result


async def _scheduler(
    args, async_frontier, raw_frontier, queue, records, url_to_tokens,
    stop_event, active_workers_ref, slot_semaphore, sampler_state,
):
    idle_loops = 0
    claims_issued = 0
    idle_time_total = 0.0

    while not stop_event.is_set():
        if slot_semaphore is not None:
            await slot_semaphore.acquire()

        try:
            claim = await async_frontier.get_next_url()
        except FrontierUnavailable as e:
            print(f"FATAL: frontier unavailable during scheduling: {e}", file=sys.stderr)
            stop_event.set()
            if slot_semaphore is not None:
                slot_semaphore.release()
            break

        if claim:
            idle_loops = 0
            now = time.time()
            records[claim.token] = {
                "token": claim.token,
                "url": claim.url,
                "attempt": claim.attempt,
                "claimed_at": now,
                "lease_expires_at": claim.lease_expires_at,
                "dequeued_at": None,
                "heartbeat_renewals": 0,
                "completed_at": None,
                "completion_result": None,
                "claim_lost": False,
                "lost_at": None,
            }
            url_to_tokens.setdefault(claim.url, []).append(claim.token)
            claims_issued += 1
            sampler_state["claims_issued"] = claims_issued
            await queue.put(claim)  # blocks here if queue_maxsize > 0 and full
            continue

        if slot_semaphore is not None:
            slot_semaphore.release()

        try:
            pending = await async_frontier.has_pending()
        except FrontierUnavailable:
            pending = True  # fail safe: keep looping rather than stopping early

        if queue.empty() and active_workers_ref[0] == 0 and not pending:
            idle_loops += 1
            if idle_loops >= args.idle_loops_to_stop:
                stop_event.set()
                break
        else:
            idle_loops = 0

        t0 = time.time()
        await asyncio.sleep(args.idle_poll_interval)
        idle_time_total += time.time() - t0

    sampler_state["claims_issued"] = claims_issued
    sampler_state["scheduler_idle_time_s"] = idle_time_total


async def _worker(
    args, async_frontier, raw_frontier, queue, records, rng,
    active_workers_ref, slot_semaphore, worker_id: int, busy_time_ref: list,
):
    while True:
        claim = await queue.get()
        active_workers_ref[0] += 1
        rec = records.get(claim.token)
        t_dequeue = time.time()
        if rec is not None:
            rec["dequeued_at"] = t_dequeue

        heartbeat_interval = resolve_heartbeat_interval(None, raw_frontier.lease_ttl)
        latency = args.worker_latency * (1 + rng.uniform(-args.worker_latency_jitter, args.worker_latency_jitter))
        latency = max(0.0, latency)
        is_real_failure = rng.random() < args.real_failure_rate

        try:
            work_coro = asyncio.sleep(latency)
            _, claim = await run_with_heartbeat(async_frontier, claim, work_coro, heartbeat_interval)
            outcome = "failed" if is_real_failure else "visited"
            error = "synthetic real failure" if is_real_failure else ""
            await _complete_and_record(raw_frontier, claim, outcome, error, records)
        except ClaimLostError:
            if rec is not None:
                rec["claim_lost"] = True
                rec["lost_at"] = time.time()
        except FrontierUnavailable as e:
            print(f"FATAL: frontier unavailable in worker {worker_id}: {e}", file=sys.stderr)
        finally:
            busy_time_ref[0] += time.time() - t_dequeue
            active_workers_ref[0] = max(0, active_workers_ref[0] - 1)
            if slot_semaphore is not None:
                slot_semaphore.release()
            queue.task_done()


async def _recovery_loop(async_frontier, interval: float, batch_size, stop_event, sweep_log):
    total_reclaimed = 0
    total_requeued = 0
    while not stop_event.is_set():
        try:
            reclaimed, requeued = await async_frontier.reclaim_and_promote(batch_size)
            total_reclaimed += reclaimed
            total_requeued += requeued
            sweep_log.append({"t": time.time(), "reclaimed": reclaimed, "requeued": requeued})
        except FrontierUnavailable as e:
            sweep_log.append({"t": time.time(), "error": str(e)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    return total_reclaimed, total_requeued


async def _sampler_loop(async_frontier, queue, interval: float, stop_event, samples: list):
    while not stop_event.is_set():
        try:
            counts = await async_frontier.get_status_counts()
        except FrontierUnavailable:
            counts = None
        samples.append({"t": time.time(), "local_queue_depth": queue.qsize(), "redis_status": counts})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _redis_evalsha_count(conn) -> int:
    try:
        stats = conn.info("commandstats")
        entry = stats.get("cmdstat_evalsha") or stats.get("cmdstat_evalsha_ro")
        return int(entry["calls"]) if entry else 0
    except Exception:
        return -1  # NOT TESTED marker: commandstats unavailable in this Redis build/config


def _compute_lifecycle_metrics(records: dict, url_to_tokens: dict, seeded_urls: list, raw_frontier) -> dict:
    pre_processing_lease_expirations = 0  # category B/C-merge: superseded, never dequeued at all
    reclaimed_during_processing = 0       # category C: dequeued, then ClaimLostError mid-heartbeat
    stale_completions = 0                 # category E: worker's own completion call rejected as stale
    eventually_processed_after_reclaim = 0  # category D
    successful_completions = 0
    still_pending_at_run_end = 0          # last token for a URL, never resolved -- run ended first

    claim_to_start_latencies = []   # dequeued_at - claimed_at, for tokens that were dequeued
    processing_latencies = []       # completed_at - dequeued_at, for tokens that completed normally

    per_url_had_early_disruption = set()  # urls with >=1 pre-processing-expiration or reclaimed-mid-processing token

    for url, tokens in url_to_tokens.items():
        for i, token in enumerate(tokens):
            rec = records[token]
            touched_by_worker = rec["dequeued_at"] is not None
            has_next = i + 1 < len(tokens)
            next_claimed_at = records[tokens[i + 1]]["claimed_at"] if has_next else None

            if rec["dequeued_at"] is not None and rec["completed_at"] is not None:
                claim_to_start_latencies.append(rec["dequeued_at"] - rec["claimed_at"])
                processing_latencies.append(rec["completed_at"] - rec["dequeued_at"])

            if rec["completion_result"] == "stale":
                stale_completions += 1
                per_url_had_early_disruption.add(url)
            elif rec["completion_result"] == "visited":
                successful_completions += 1
                if url in per_url_had_early_disruption:
                    eventually_processed_after_reclaim += 1

            if rec["claim_lost"]:
                per_url_had_early_disruption.add(url)
                # The local FIFO queue has no idea a claim died while it sat
                # waiting -- ClaimLostError only surfaces once a worker
                # finally dequeues it and its first heartbeat check runs.
                # If a *newer* claim on the same URL was already issued
                # before this token was even dequeued, this token was dead
                # on arrival (never meaningfully processed at all) -- the
                # late ClaimLostError is just where that fact was finally
                # discovered, not evidence the worker was genuinely
                # mid-flight when the reclaim happened.
                if touched_by_worker and (not has_next or next_claimed_at > rec["dequeued_at"]):
                    reclaimed_during_processing += 1
                else:
                    pre_processing_lease_expirations += 1
            elif not touched_by_worker and rec["completion_result"] is None:
                if has_next:
                    pre_processing_lease_expirations += 1
                    per_url_had_early_disruption.add(url)
                else:
                    still_pending_at_run_end += 1

    # Category F: URL's terminal state is failed_permanent (per get_status_counts'
    # authoritative urls:failed_permanent set, checked below) AND no token of that
    # URL was ever dequeued by a worker at all -- i.e. it was never actually
    # fetched, only reclaim-driven attempt increments exhausted its retry budget.
    failed_permanent_urls_ever_dequeued = set()
    for url, tokens in url_to_tokens.items():
        if any(records[t]["dequeued_at"] is not None for t in tokens):
            failed_permanent_urls_ever_dequeued.add(url)

    return {
        "pre_processing_lease_expirations": pre_processing_lease_expirations,
        "reclaimed_during_processing": reclaimed_during_processing,
        "stale_completions": stale_completions,
        "eventually_processed_after_reclaim": eventually_processed_after_reclaim,
        "successful_completions": successful_completions,
        "still_pending_at_run_end": still_pending_at_run_end,
        "urls_with_early_disruption": len(per_url_had_early_disruption),
        "claim_to_start_latency_s": common.latency_stats(claim_to_start_latencies),
        "processing_latency_s": common.latency_stats(processing_latencies),
        "_failed_permanent_urls_ever_dequeued": sorted(failed_permanent_urls_ever_dequeued),
    }


async def run_overload(args) -> dict:
    if args.redis_db == 0:
        raise SystemExit("Refusing to run: --redis-db 0 is production. Use db 1/2/etc.")

    args.namespace = _namespace_for(args, "n6_backpressure")
    kwargs = common.frontier_kwargs_from_args(args)
    raw_frontier = common.build_frontier("redis", domain_scan_limit=max(250, args.domains + 10), **kwargs)
    raw_frontier.clear()

    isolate_dir = None
    common.isolate_blacklist(isolate_dir)

    renewal_counts: dict = {}
    async_frontier = _CountingAsyncFrontier(raw_frontier, renewal_counts)

    rng = random.Random(args.seed)
    priority_fn = lambda _rng: 5
    synthetic = common.make_synthetic_urls(
        args.urls, args.domains, priority_fn, rng,
        run_id=args.namespace, domain_prefix="n6sched",
    )

    t_seed_start = time.time()
    for url, priority in synthetic:
        raw_frontier.add_url(url, priority=priority)
    seed_elapsed = time.time() - t_seed_start
    seeded_urls = [u for u, _ in synthetic]

    queue: asyncio.Queue = asyncio.Queue(maxsize=args.queue_maxsize) if args.queue_maxsize > 0 else asyncio.Queue()
    records: dict = {}
    url_to_tokens: dict = {}
    stop_event = asyncio.Event()
    active_workers_ref = [0]
    busy_time_ref = [0.0]
    sweep_log: list = []
    depth_samples: list = []
    sampler_state: dict = {"claims_issued": 0, "scheduler_idle_time_s": 0.0}
    slot_semaphore = asyncio.Semaphore(args.concurrency) if args.slot_gate else None

    evalsha_before = _redis_evalsha_count(raw_frontier.redis_conn)
    resource_monitor = common.ResourceMonitor(redis_conn=raw_frontier.redis_conn, interval=args.sample_interval)

    t_run_start = time.time()
    with resource_monitor:
        scheduler_task = asyncio.create_task(
            _scheduler(args, async_frontier, raw_frontier, queue, records, url_to_tokens,
                       stop_event, active_workers_ref, slot_semaphore, sampler_state)
        )
        worker_tasks = [
            asyncio.create_task(
                _worker(args, async_frontier, raw_frontier, queue, records, random.Random(args.seed + 1 + i),
                        active_workers_ref, slot_semaphore, i, busy_time_ref)
            )
            for i in range(args.concurrency)
        ]
        recovery_task = asyncio.create_task(
            _recovery_loop(async_frontier, args.recovery_interval, args.reclaim_batch_size, stop_event, sweep_log)
        )
        sampler_task = asyncio.create_task(
            _sampler_loop(async_frontier, queue, args.sample_interval, stop_event, depth_samples)
        )

        timed_out = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.max_wall_time)
        except asyncio.TimeoutError:
            timed_out = True
            stop_event.set()

        scheduler_task.cancel()
        for t in worker_tasks:
            t.cancel()
        recovery_task.cancel()
        sampler_task.cancel()
        results = await asyncio.gather(scheduler_task, *worker_tasks, recovery_task, sampler_task, return_exceptions=True)
        recovery_totals = results[-2] if isinstance(results[-2], tuple) else (None, None)

    t_run_end = time.time()
    evalsha_after = _redis_evalsha_count(raw_frontier.redis_conn)

    final_status = raw_frontier.get_status_counts()
    lifecycle = _compute_lifecycle_metrics(records, url_to_tokens, seeded_urls, raw_frontier)

    inflight_samples = [s["redis_status"]["inflight"] for s in depth_samples if s["redis_status"]]
    queue_depth_samples = [s["local_queue_depth"] for s in depth_samples]

    elapsed = t_run_end - t_run_start
    claims_issued = sampler_state["claims_issued"]
    completed = lifecycle["successful_completions"]

    result = {
        "run": {"tool": "scheduler_backpressure", "mode": "overload", "timestamp": common.now_iso(),
                "label": args.label, "namespace": args.namespace, "redis_db": args.redis_db},
        "config": {
            "concurrency": args.concurrency, "urls_seeded": args.urls, "domains": args.domains,
            "worker_latency_s": args.worker_latency, "worker_latency_jitter": args.worker_latency_jitter,
            "real_failure_rate": args.real_failure_rate, "recovery_interval_s": args.recovery_interval,
            "queue_maxsize": args.queue_maxsize if args.queue_maxsize > 0 else "unbounded",
            "slot_gate": args.slot_gate, "lease_ttl_s": args.lease_ttl, "rate_limit_s": args.rate_limit,
            "max_retries": args.max_retries, "reclaim_batch_size": args.reclaim_batch_size,
        },
        "timing": {
            "seed_elapsed_s": seed_elapsed, "run_elapsed_s": elapsed, "timed_out": timed_out,
            "scheduler_idle_time_s": sampler_state["scheduler_idle_time_s"],
        },
        "throughput": {
            "claims_issued_total": claims_issued,
            "claim_rate_per_s": claims_issued / elapsed if elapsed > 0 else None,
            "successful_completions": completed,
            "completion_rate_per_s": completed / elapsed if elapsed > 0 else None,
            "worker_utilization": (busy_time_ref[0] / (args.concurrency * elapsed)) if elapsed > 0 else None,
        },
        "lifecycle": {k: v for k, v in lifecycle.items() if not k.startswith("_")},
        "final_status_counts": final_status,
        "queue_depth": {
            "peak": max(queue_depth_samples) if queue_depth_samples else None,
            "avg": (sum(queue_depth_samples) / len(queue_depth_samples)) if queue_depth_samples else None,
            "samples": len(queue_depth_samples),
        },
        "redis_inflight": {
            "peak": max(inflight_samples) if inflight_samples else None,
            "avg": (sum(inflight_samples) / len(inflight_samples)) if inflight_samples else None,
            "peak_vs_concurrency_ratio": (max(inflight_samples) / args.concurrency) if inflight_samples else None,
        },
        "recovery": {
            "sweep_count": len(sweep_log),
            "total_reclaimed": sum(s.get("reclaimed", 0) for s in sweep_log),
            "total_requeued": sum(s.get("requeued", 0) for s in sweep_log),
        },
        "resource_usage": resource_monitor.summary(),
        "redis_evalsha_calls": {
            "before": evalsha_before, "after": evalsha_after,
            "delta": (evalsha_after - evalsha_before) if evalsha_before >= 0 and evalsha_after >= 0 else None,
            "note": "aggregate across ALL Lua scripts (claim_next/complete/mark_deferred/renew/reclaim), "
                    "server-wide INFO commandstats counter -- MEASURED if delta is non-negative, "
                    "NOT TESTED (-1) if this Redis build/config doesn't expose commandstats",
        },
        "category_f_check": {
            "failed_permanent_count": final_status["failed_permanent"],
            "failed_permanent_urls_ever_dequeued_by_a_worker": len(lifecycle["_failed_permanent_urls_ever_dequeued"]),
            "note": "if failed_permanent_count > 0 and failed_permanent_urls_ever_dequeued_by_a_worker == 0, "
                    "every permanently-failed URL in this run was never actually fetched by any worker -- "
                    "proves category F (retry budget exhausted purely by reclaim/queueing, no real "
                    "failure -- real_failure_rate={} in this run)".format(args.real_failure_rate),
        },
    }

    if args.dump_lifecycle:
        common.write_result({"records": list(records.values()), "url_to_tokens": url_to_tokens},
                             args.dump_lifecycle, "json")

    raw_frontier.clear()
    raw_frontier.close()
    return result


# ---------------------------------------------------------------------------
# stale mode -- deterministic, matches tests/benchmarks/crash_recovery.py's
# idiom exactly, framed for the N6 "local queue delay" narrative rather than
# a killed worker (same underlying mechanism: a claim outlives its lease
# without renewal because nothing is calling renew_claim on it yet).
# ---------------------------------------------------------------------------

def run_stale_scenario(args) -> dict:
    if args.redis_db == 0:
        raise SystemExit("Refusing to run: --redis-db 0 is production. Use db 1/2/etc.")

    args.namespace = _namespace_for(args, "n6_stale")
    kwargs = common.frontier_kwargs_from_args(args)
    frontier = common.build_frontier("redis", **kwargs)
    frontier.clear()
    common.isolate_blacklist()

    timeline: list[dict] = []
    t_start = time.time()

    def log(event, **extra):
        timeline.append({"t_offset_s": round(time.time() - t_start, 3), "event": event, **extra})

    url = f"https://n6-stale.example.test/url-{int(t_start)}"
    frontier.add_url(url, priority=5)
    log("add_url", url=url)

    # Step 1: URL A is claimed.
    claim_a = frontier.get_next_url()
    assert claim_a is not None and claim_a.url == url
    log("worker_a_claimed", token=claim_a.token, attempt=claim_a.attempt)

    # Step 2: URL A waits in the local queue -- nothing renews it, nothing
    # processes it yet (this is the exact gap Finding N5-1 identifies:
    # local-queue residency time with zero heartbeat coverage).
    log("worker_a_url_sits_in_local_queue (no renewal, no processing yet)")

    # Step 3: lease expires.
    wait_s = args.lease_ttl + args.lease_margin
    time.sleep(wait_s)
    log("lease_expired", waited_s=round(wait_s, 3))

    # Step 4: recovery reclaims URL A (this is the same call
    # core/crawler_manager.py's _recovery_loop makes on a timer).
    reclaimed, requeued = frontier.reclaim_and_promote()
    log("recovery_sweep_1_reclaim", reclaimed=reclaimed, requeued=requeued)

    time.sleep(args.base_backoff + 0.2)
    reclaimed2, requeued2 = frontier.reclaim_and_promote()
    log("recovery_sweep_2_promote", reclaimed=reclaimed2, requeued=requeued2)

    # A second worker (or the same process's scheduler) claims the promoted
    # URL with a new token B, attempt+1 -- this models the newer claim that
    # now legitimately owns the URL.
    claim_b = frontier.get_next_url()
    got_claim_b = claim_b is not None and claim_b.url == url
    log("worker_b_claimed", got=got_claim_b,
        token=(claim_b.token if got_claim_b else None),
        attempt=(claim_b.attempt if got_claim_b else None))

    # Step 5/6: URL A is "then allowed to start processing" -- i.e. worker A,
    # having finally reached the front of its local queue, now actually runs
    # its simulated fetch and finishes.
    status_before_a_completion = frontier.get_status_counts()

    # Step 7: worker A attempts completion with the OLD token.
    result_a = frontier._complete(claim_a, "visited", "")
    status_after_a_completion = frontier.get_status_counts()
    log("worker_a_stale_completion_attempted", result=result_a)

    state_untouched_by_stale_completion = status_before_a_completion == status_after_a_completion

    # Worker B completes normally with the current token -- proves category D
    # (the URL is still eventually, correctly processed).
    result_b = None
    if got_claim_b:
        result_b = frontier._complete(claim_b, "visited", "")
        log("worker_b_completed", result=result_b)

    final_status = frontier.get_status_counts()
    frontier.clear()
    frontier.close()

    pass_conditions = {
        "worker_a_completion_rejected_as_stale": result_a == "stale",
        "newer_claim_state_untouched_by_stale_completion": state_untouched_by_stale_completion,
        "url_eventually_correctly_processed_by_worker_b": got_claim_b and result_b == "visited",
    }
    outcome = "PASS" if all(pass_conditions.values()) else "FAIL"

    return {
        "run": {"tool": "scheduler_backpressure", "mode": "stale", "timestamp": common.now_iso(),
                "namespace": args.namespace, "redis_db": args.redis_db},
        "url": url,
        "timeline": timeline,
        "worker_a_claim": {"token": claim_a.token, "attempt": claim_a.attempt},
        "worker_b_claim": ({"token": claim_b.token, "attempt": claim_b.attempt} if got_claim_b else None),
        "worker_a_completion_result": result_a,
        "worker_b_completion_result": result_b,
        "final_status_counts": final_status,
        "pass_conditions": pass_conditions,
        "outcome": outcome,
    }


def main() -> None:
    args = parse_args()

    if args.mode == "overload":
        result = asyncio.run(run_overload(args))
        common.write_result(result, args.output, args.format)
    else:
        result = run_stale_scenario(args)
        common.write_result(result, args.output, args.format)
        if result["outcome"] == "FAIL":
            sys.exit(1)


if __name__ == "__main__":
    main()
