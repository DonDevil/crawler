#!/usr/bin/env python3
"""Media evidence store smoke benchmark (Redis backend only).

Small, deterministic, manually-run script -- NOT a load-test campaign (see
docs/architecture/media-evidence-step1.md, "Benchmarking"). Measures insert
(`record_media_link`) throughput/latency, claim/complete throughput/latency
across `--claim-workers` real OS threads, duplicate-claim count (must be
zero), and Redis memory growth for one deterministic run.

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
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
    p.add_argument("--assets", type=int, default=500, help="Number of synthetic media assets to insert")
    p.add_argument("--claim-workers", type=int, default=4, help="Concurrent OS threads claiming/completing jobs")
    p.add_argument("--redis-host", default="localhost")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--redis-db", type=int, default=DEFAULT_REDIS_DB)
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument("--fingerprint-lease-ttl", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--no-clear", action="store_true")
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


def main() -> None:
    args = parse_args()
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
