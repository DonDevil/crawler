#!/usr/bin/env python3
"""Media evidence store benchmarks (Redis backend only).

Small, deterministic, manually-run script -- NOT a load-test campaign (see
docs/architecture/media-evidence-step1.md, "Benchmarking"). Two modes:

- `--mode smoke` (default, original behavior): insert (`record_media_link`)
  throughput/latency, claim/complete throughput/latency across
  `--claim-workers` real OS threads, duplicate-claim count (must be zero),
  and Redis memory growth for one deterministic run.
- `--mode offload`: compares a synchronous `record_media_link`/
  `record_manifest_variants` call made directly on the asyncio event loop
  against the same call made through `core.media_evidence_executor.
  AsyncMediaEvidence` (`asyncio.to_thread`-offloaded), under concurrent
  synthetic workers -- see docs/architecture/fetch-extractor-audit.md
  §8/§14. This isolates Media Evidence overhead only: synthetic workers,
  no real HTTP/network fetch, synthetic/local Redis data only. It exists to
  measure whether the offload fix has a real effect, not to assert one --
  see this module's `run_offload_comparison` docstring.

Does not match pytest's `test_*.py`/`*_test.py` discovery pattern on
purpose -- this is a CLI tool you run by hand, matching the convention
established by tests/benchmarks/frontier_benchmark.py and friends. See
tests/benchmarks/README.md for that convention; this script intentionally
mirrors its output shape (via `common.latency_stats`/`write_result`) rather
than inventing a new one.

Examples:
    python tests/benchmarks/media_evidence_benchmark.py
    python tests/benchmarks/media_evidence_benchmark.py --assets 2000 --claim-workers 8 \\
        --output /tmp/media_evidence_bench.json
    python tests/benchmarks/media_evidence_benchmark.py --mode offload \\
        --workers 25 --pages-per-worker 20 --media-per-page 3
    python tests/benchmarks/media_evidence_benchmark.py --mode offload \\
        --artificial-latency-ms 5 --output /tmp/offload_bench.json
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.media_evidence_executor import AsyncMediaEvidence  # noqa: E402
from storage.media_evidence_store import FingerprintResult  # noqa: E402
from storage.redis_media_evidence_store import RedisMediaEvidenceStore  # noqa: E402

# Distinct from production (db 0, ns "evidence"), the pytest Redis suite
# (db 1, ns "test_evidence"/"test_evidence_mp"), and the frontier's own
# benchmark default (db 2, ns "bench") -- own namespace under the shared
# benchmark db so a run here can never collide with a frontier benchmark
# run against the same db.
DEFAULT_REDIS_DB = 2
DEFAULT_NAMESPACE = "bench_evidence"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--mode",
        choices=["smoke", "offload"],
        default="smoke",
        help="smoke: original insert/claim/complete benchmark (default). "
        "offload: sync-on-event-loop vs asyncio.to_thread-offloaded comparison.",
    )
    p.add_argument("--assets", type=int, default=500, help="[smoke mode] Number of synthetic media assets to insert")
    p.add_argument("--claim-workers", type=int, default=4, help="[smoke mode] Concurrent OS threads claiming/completing jobs")
    p.add_argument("--redis-host", default="localhost")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--redis-db", type=int, default=DEFAULT_REDIS_DB)
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument("--fingerprint-lease-ttl", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--no-clear", action="store_true")
    p.add_argument(
        "--workers",
        type=int,
        default=25,
        help="[offload mode] concurrent synthetic async workers (matches crawler.concurrency default)",
    )
    p.add_argument(
        "--pages-per-worker",
        type=int,
        default=20,
        help="[offload mode] synthetic pages processed per worker",
    )
    p.add_argument(
        "--media-per-page",
        type=int,
        default=3,
        help="[offload mode] media links recorded per page (record_media_link calls)",
    )
    p.add_argument(
        "--tick-interval",
        type=float,
        default=0.01,
        help="[offload mode] event-loop responsiveness probe interval in seconds",
    )
    p.add_argument(
        "--artificial-latency-ms",
        type=float,
        default=0.0,
        help="[offload mode] optional synthetic per-call latency (ms) injected before each "
        "record_media_link/record_manifest_variants call in both phases, to approximate a "
        "busier/non-loopback Redis beyond this benchmark's local sub-ms round trip",
    )
    return p.parse_args()


def _build_store(args: argparse.Namespace) -> RedisMediaEvidenceStore:
    return RedisMediaEvidenceStore(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        namespace=args.namespace,
        fingerprint_lease_ttl=args.fingerprint_lease_ttl,
    )


def _run_insert_phase(store: RedisMediaEvidenceStore, args: argparse.Namespace, rng: random.Random) -> dict:
    latencies = []
    start = time.time()
    for i in range(args.assets):
        t0 = time.time()
        store.record_media_link(
            url=f"https://cdn.bench.example/movie-{i}.mp4",
            source_page=f"https://piracy.bench.example/watch/{i}",
            media_type="video",
            priority=rng.randint(1, 10),
        )
        latencies.append(time.time() - t0)
    elapsed = time.time() - start
    return {
        "elapsed_s": elapsed,
        "throughput_per_s": args.assets / elapsed if elapsed > 0 else None,
        "latency": common.latency_stats(latencies),
    }


def _run_claim_complete_phase(args: argparse.Namespace) -> tuple[dict, dict, int]:
    claim_latencies: list[float] = []
    complete_latencies: list[float] = []
    claimed_ids: list[str] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        wstore = _build_store(args)
        try:
            while True:
                t0 = time.time()
                job = wstore.claim_next_fingerprint_job(worker_id=f"bench-worker-{worker_id}")
                claim_dt = time.time() - t0
                if job is None:
                    return
                with lock:
                    claim_latencies.append(claim_dt)
                    claimed_ids.append(job.asset_id)

                t0 = time.time()
                wstore.complete_fingerprint_job(
                    job.asset_id, job.token, result=FingerprintResult(aggregate_decision="uncertain")
                )
                with lock:
                    complete_latencies.append(time.time() - t0)
        finally:
            wstore.close()

    start = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.claim_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    duplicate_claims = len(claimed_ids) - len(set(claimed_ids))

    claim_result = {
        "elapsed_s": elapsed,
        "claimed": len(claimed_ids),
        "throughput_per_s": len(claimed_ids) / elapsed if elapsed > 0 else None,
        "duplicate_claims": duplicate_claims,
        "latency": common.latency_stats(claim_latencies),
    }
    complete_result = {
        "completed": len(complete_latencies),
        "throughput_per_s": len(complete_latencies) / elapsed if elapsed > 0 else None,
        "latency": common.latency_stats(complete_latencies),
    }
    return claim_result, complete_result, duplicate_claims


# ---------------------------------------------------------------------------
# Offload comparison (docs/architecture/fetch-extractor-audit.md §8/§14)
# ---------------------------------------------------------------------------


def _wrap_with_latency(store: RedisMediaEvidenceStore, latency_ms: float) -> None:
    """Inject a fixed synthetic delay before each hot-path call, applied
    identically to both phases -- lets `--artificial-latency-ms` approximate
    a busier/non-loopback Redis beyond this benchmark's local sub-ms round
    trip, without changing which thread the delay executes on (that's the
    whole point of the comparison)."""
    if latency_ms <= 0:
        return
    delay_s = latency_ms / 1000.0

    original_record_media_link = store.record_media_link
    original_record_manifest_variants = store.record_manifest_variants

    def record_media_link(*args, **kwargs):
        time.sleep(delay_s)
        return original_record_media_link(*args, **kwargs)

    def record_manifest_variants(*args, **kwargs):
        time.sleep(delay_s)
        return original_record_manifest_variants(*args, **kwargs)

    store.record_media_link = record_media_link
    store.record_manifest_variants = record_manifest_variants


def _measure_redis_ping(redis_conn, samples: int = 20) -> dict:
    latencies = []
    for _ in range(samples):
        t0 = time.time()
        redis_conn.ping()
        latencies.append(time.time() - t0)
    return common.latency_stats(latencies)


def _make_sync_op(
    store: RedisMediaEvidenceStore, media_per_page: int, rng: random.Random
) -> Callable[[int, int], Awaitable[int]]:
    """The bug this benchmark measures: a synchronous, unwrapped
    `record_media_link` call made directly inside an `async def` -- exactly
    what every crawler engine's `worker()` did before this fix (see
    docs/architecture/fetch-extractor-audit.md §8). No `await`, no
    `asyncio.to_thread`: the call blocks the event loop's one OS thread for
    its full duration."""

    async def op(worker_id: int, page_idx: int) -> int:
        for m in range(media_per_page):
            store.record_media_link(
                url=f"https://bench.example/offload/sync/w{worker_id}/p{page_idx}/m{m}",
                source_page=f"https://bench.example/offload/sync/w{worker_id}/p{page_idx}",
                media_type="video",
                priority=rng.randint(1, 10),
            )
        return media_per_page

    return op


def _make_async_op(
    adapter: AsyncMediaEvidence, media_per_page: int, rng: random.Random
) -> Callable[[int, int], Awaitable[int]]:
    """The fix: the same call, routed through `AsyncMediaEvidence` so the
    blocking Redis I/O runs on `asyncio.to_thread`'s shared executor thread
    instead of the event-loop thread."""

    async def op(worker_id: int, page_idx: int) -> int:
        for m in range(media_per_page):
            await adapter.record_media_link(
                url=f"https://bench.example/offload/async/w{worker_id}/p{page_idx}/m{m}",
                source_page=f"https://bench.example/offload/async/w{worker_id}/p{page_idx}",
                media_type="video",
                priority=rng.randint(1, 10),
            )
        return media_per_page

    return op


async def _run_phase(
    op: Callable[[int, int], Awaitable[int]],
    workers: int,
    pages_per_worker: int,
    tick_interval: float,
) -> tuple[float, int, list[float]]:
    """Run `workers` concurrent synthetic worker coroutines (each processing
    `pages_per_worker` synthetic pages via `op`) alongside a ticker task that
    measures event-loop scheduling delay for the whole phase -- a tick that
    takes much longer than `tick_interval` means something blocked the loop
    during that window. This is the same technique used to detect an
    unresponsive event loop in production asyncio services.

    Returns (elapsed_seconds, completed_ops, per_tick_scheduling_delay_seconds).
    """
    delays: list[float] = []

    async def ticker() -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                t0 = loop.time()
                await asyncio.sleep(tick_interval)
                delays.append(loop.time() - t0 - tick_interval)
        except asyncio.CancelledError:
            pass

    async def worker(worker_id: int) -> int:
        ops = 0
        for page_idx in range(pages_per_worker):
            ops += await op(worker_id, page_idx)
        return ops

    ticker_task = asyncio.create_task(ticker())
    start = time.time()
    results = await asyncio.gather(*(worker(i) for i in range(workers)))
    elapsed = time.time() - start
    ticker_task.cancel()
    await asyncio.gather(ticker_task, return_exceptions=True)

    return elapsed, sum(results), delays


def _phase_result(
    label: str,
    elapsed: float,
    ops: int,
    delays: list[float],
    workers: int,
    pages_per_worker: int,
    media_per_page: int,
    monitor: "common.ResourceMonitor",
    ping_stats: dict,
) -> dict:
    stall_threshold_s = 0.01  # 10ms: a tick this late means the loop was blocked, not just scheduled late
    return {
        "label": label,
        "workers": workers,
        "pages_per_worker": pages_per_worker,
        "media_per_page": media_per_page,
        "elapsed_s": elapsed,
        "completed_ops": ops,
        "throughput_ops_per_s": ops / elapsed if elapsed > 0 else None,
        "event_loop_scheduling_delay_s": common.latency_stats(delays) if delays else {},
        "event_loop_ticks_sampled": len(delays),
        "event_loop_stalls_over_10ms": sum(1 for d in delays if d > stall_threshold_s),
        "redis_ping_latency_s": ping_stats,
        "resource": monitor.summary(),
    }


def run_offload_comparison(args: argparse.Namespace) -> dict:
    """Compare a synchronous on-event-loop Media Evidence call against the
    same call routed through `AsyncMediaEvidence` (`asyncio.to_thread`),
    under `--workers` concurrent synthetic workers each processing
    `--pages-per-worker` synthetic pages of `--media-per-page` media links.

    Isolates Media Evidence overhead from HTTP/network latency entirely --
    no real fetch happens, only synthetic URLs against a real local Redis
    (or `--artificial-latency-ms` for a synthetic non-loopback approximation).

    This function measures; it does not assert a throughput improvement.
    Local loopback Redis round trips are typically sub-millisecond, so at
    modest concurrency the wall-clock throughput delta between the two
    phases may be small even though the mechanism (blocking the event loop)
    is real -- the honest signal to look at is
    `event_loop_scheduling_delay_s`/`event_loop_stalls_over_10ms`, which
    directly measures whether other coroutines could make progress while a
    Media Evidence call was in flight, independent of how fast Redis itself
    happened to respond on this machine.
    """
    sync_store = RedisMediaEvidenceStore(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        namespace=f"{args.namespace}_offload_sync",
        fingerprint_lease_ttl=args.fingerprint_lease_ttl,
    )
    async_store = RedisMediaEvidenceStore(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        namespace=f"{args.namespace}_offload_async",
        fingerprint_lease_ttl=args.fingerprint_lease_ttl,
    )

    if not args.no_clear:
        sync_store.clear()
        async_store.clear()

    if args.artificial_latency_ms:
        _wrap_with_latency(sync_store, args.artificial_latency_ms)
        _wrap_with_latency(async_store, args.artificial_latency_ms)

    adapter = AsyncMediaEvidence(async_store)
    rng_sync = random.Random(args.seed)
    rng_async = random.Random(args.seed)
    sync_op = _make_sync_op(sync_store, args.media_per_page, rng_sync)
    async_op = _make_async_op(adapter, args.media_per_page, rng_async)

    sync_ping = _measure_redis_ping(sync_store.redis_conn)
    with common.ResourceMonitor(redis_conn=sync_store.redis_conn, interval=0.1) as sync_monitor:
        sync_elapsed, sync_ops, sync_delays = asyncio.run(
            _run_phase(sync_op, args.workers, args.pages_per_worker, args.tick_interval)
        )

    async_ping = _measure_redis_ping(async_store.redis_conn)
    with common.ResourceMonitor(redis_conn=async_store.redis_conn, interval=0.1) as async_monitor:
        async_elapsed, async_ops, async_delays = asyncio.run(
            _run_phase(async_op, args.workers, args.pages_per_worker, args.tick_interval)
        )

    result = {
        "timestamp": common.now_iso(),
        "mode": "offload-comparison",
        "note": (
            "Isolated Media Evidence overhead only -- no HTTP/network fetch involved. "
            "Synthetic workers against local Redis; synthetic/local data only."
        ),
        "artificial_latency_ms": args.artificial_latency_ms,
        "tick_interval_s": args.tick_interval,
        "sync_on_event_loop": _phase_result(
            "sync_on_event_loop", sync_elapsed, sync_ops, sync_delays,
            args.workers, args.pages_per_worker, args.media_per_page, sync_monitor, sync_ping,
        ),
        "async_offloaded": _phase_result(
            "async_offloaded", async_elapsed, async_ops, async_delays,
            args.workers, args.pages_per_worker, args.media_per_page, async_monitor, async_ping,
        ),
    }

    if not args.no_clear:
        sync_store.clear()
        async_store.clear()
    sync_store.close()
    async_store.close()

    return result


def main() -> None:
    args = parse_args()

    if args.mode == "offload":
        result = run_offload_comparison(args)
        common.write_result(result, args.output, fmt=args.format)
        return

    rng = random.Random(args.seed)

    store = _build_store(args)
    if not args.no_clear:
        store.clear()

    mem_before = store.redis_conn.info("memory").get("used_memory")

    insert_result = _run_insert_phase(store, args, rng)
    claim_result, complete_result, duplicate_claims = _run_claim_complete_phase(args)

    mem_after = store.redis_conn.info("memory").get("used_memory")
    mem_delta = (mem_after - mem_before) if (mem_before is not None and mem_after is not None) else None

    result = {
        "timestamp": common.now_iso(),
        "assets": args.assets,
        "claim_workers": args.claim_workers,
        "insert": insert_result,
        "claim": claim_result,
        "complete": complete_result,
        "redis_memory_bytes": {"before": mem_before, "after": mem_after, "delta": mem_delta},
        "status_counts": store.get_status_counts(),
    }

    common.write_result(result, args.output, fmt=args.format)

    if not args.no_clear:
        store.clear()
    store.close()

    if duplicate_claims:
        print(f"\nFAIL: {duplicate_claims} duplicate successful claims", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
