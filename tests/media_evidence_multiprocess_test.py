"""Multi-process claim-safety test for RedisMediaEvidenceStore.

Distributed correctness (exactly one worker ever owns a given fingerprint
job) must be verified across independent OS processes, not just asyncio
tasks or threads in one process -- a single process shares the GIL and one
Redis connection pool in ways that can hide races a truly distributed
fingerprinter fleet would hit (the same rationale
tests/benchmarks/distributed_benchmark.py documents for the URL frontier).

Run this test ONLY if Redis is available locally on port 6379.
"""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import time
from pathlib import Path

import pytest
import redis

from storage.media_evidence_store import FingerprintResult
from storage.redis_media_evidence_store import RedisMediaEvidenceStore

_NAMESPACE = "test_evidence_mp"


def _redis_available() -> bool:
    try:
        redis.Redis(host="localhost", port=6379, db=1).ping()
        return True
    except redis.ConnectionError:
        return False


def _worker_main(worker_id: int, result_path: str) -> None:
    """Entry point for one independent worker process: claims jobs until
    the queue is drained, immediately completing each one, and records
    every asset_id it successfully claimed."""
    store = RedisMediaEvidenceStore(redis_host="localhost", redis_port=6379, redis_db=1, namespace=_NAMESPACE)

    claimed_ids: list[str] = []
    idle_polls = 0
    while idle_polls < 20:
        job = store.claim_next_fingerprint_job(worker_id=f"worker-{worker_id}")
        if job is None:
            idle_polls += 1
            time.sleep(0.02)
            continue
        idle_polls = 0
        claimed_ids.append(job.asset_id)
        store.complete_fingerprint_job(job.asset_id, job.token, result=FingerprintResult(aggregate_decision="uncertain"))

    store.close()
    Path(result_path).write_text(json.dumps({"worker_id": worker_id, "claimed_ids": claimed_ids}))


def _run_multiprocess_claim_safety(worker_count: int, job_count: int) -> None:
    seed_store = RedisMediaEvidenceStore(redis_host="localhost", redis_port=6379, redis_db=1, namespace=_NAMESPACE)
    seed_store.clear()
    for i in range(job_count):
        seed_store.record_media_link(url=f"https://cdn.example/mp-{worker_count}-{i}.mp4", media_type="video")
    seed_store.close()

    with tempfile.TemporaryDirectory(prefix="media_evidence_mp_") as tmp_dir:
        result_paths = [str(Path(tmp_dir) / f"worker_{i}.json") for i in range(worker_count)]
        procs = [
            multiprocessing.Process(target=_worker_main, args=(i, result_paths[i])) for i in range(worker_count)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert not p.is_alive(), "worker process did not finish in time"

        all_claimed: list[str] = []
        for path in result_paths:
            data = json.loads(Path(path).read_text())
            all_claimed.extend(data["claimed_ids"])

    verify_store = RedisMediaEvidenceStore(redis_host="localhost", redis_port=6379, redis_db=1, namespace=_NAMESPACE)
    try:
        assert len(all_claimed) == job_count, f"expected {job_count} total claims, got {len(all_claimed)}"
        duplicate_claims = len(all_claimed) - len(set(all_claimed))
        assert duplicate_claims == 0, f"{duplicate_claims} duplicate successful claims across worker processes"

        counts = verify_store.get_status_counts()
        assert counts["completed"] == job_count
        assert counts["queued"] == 0
        assert counts["claimed"] == 0
    finally:
        verify_store.clear()
        verify_store.close()


@pytest.mark.parametrize("worker_count", [2, 4, 8])
def test_no_duplicate_claims_across_independent_processes(worker_count: int):
    if not _redis_available():
        pytest.skip("Redis not available on localhost:6379")
    _run_multiprocess_claim_safety(worker_count=worker_count, job_count=worker_count * 10)
