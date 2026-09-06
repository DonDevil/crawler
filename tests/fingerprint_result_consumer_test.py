"""Tests for bridge/fingerprint_result_consumer.py -- the reverse-direction
half of the crawler<->fingerprinter bridge: consumes the fingerprinter's
`fingerprint:results:stream:{priority}` and completes the corresponding
crawler-side forwarded job via `complete_forwarded_fingerprint_job`.

Run against real local Redis (test DB 1, this repo's existing Redis-test
convention, same as tests/bridge_test.py) -- skip cleanly if unavailable.
Never touches production DB 0. Synthetic fingerprinter-side result
records/events are written directly via raw Redis calls (mirroring
tests/bridge_test.py's `_register_target` technique) -- this repo's tests
must not import fingerprinter Python code, exactly like production code.
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import pytest
import redis

from bridge.fingerprint_result_adapter import result_key, results_stream_key
from bridge.fingerprint_result_consumer import FingerprintResultConsumer
from storage.media_evidence_store import MediaEvidenceUnavailable
from storage.redis_media_evidence_store import RedisMediaEvidenceStore

_TEST_JOB_PREFIX = "rc_test_"


def _unique_job_id() -> str:
    return f"{_TEST_JOB_PREFIX}{uuid.uuid4().hex}"


def _unique_priority() -> str:
    """A private, per-test priority stream name -- never the real `default`
    stream -- so tests never interfere with each other or with production."""
    return f"rc_test_{uuid.uuid4().hex}"


def _write_result(
    conn: "redis.Redis",
    priority: str,
    *,
    job_id: str,
    media_evidence_id: str,
    target_id: str = "target-1",
    target_version: str = "v1",
    decision: str,
    worker_id: str = "worker-1",
    confidence: float | None = None,
    evidence: str | None = None,
    algorithm: str = "dinov2",
) -> str:
    """Write a synthetic result hash + stream event matching the
    fingerprinter's actual schema (work_queue.results.ResultRecord's
    to_hash_fields/to_event_fields), byte-for-byte, without importing that
    module -- the same posture bridge/fingerprint_result_adapter.py takes
    in production code."""
    now = time.time()
    hash_fields = {
        "job_id": job_id,
        "media_evidence_id": media_evidence_id,
        "target_id": target_id,
        "target_version": target_version,
        "attempt": "1",
        "worker_id": worker_id,
        "result_version": "1",
        "decision": decision,
        "algorithm": algorithm,
        "processing_started_at": str(now - 1),
        "processing_completed_at": str(now),
        "processing_duration": "1.0",
    }
    if confidence is not None:
        hash_fields["confidence"] = str(confidence)
    if evidence is not None:
        hash_fields["evidence"] = evidence
    conn.hset(result_key(job_id), mapping=hash_fields)

    event_fields = {
        "job_id": job_id,
        "media_evidence_id": media_evidence_id,
        "target_id": target_id,
        "attempt": "1",
        "decision": decision,
        "result_version": "1",
        "worker_id": worker_id,
        "completed_at": str(now),
    }
    return conn.xadd(results_stream_key(priority), event_fields)


@pytest.fixture
def conn():
    try:
        c = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        c.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")
    yield c
    for key in c.keys(f"fingerprint:job:{_TEST_JOB_PREFIX}*"):
        c.delete(key)
    c.close()


@pytest.fixture
def evidence_store(conn):
    store = RedisMediaEvidenceStore(
        redis_host="localhost", redis_port=6379, redis_db=1, namespace="test_result_consumer_evidence"
    )
    store.clear()
    yield store
    store.clear()
    store.close()


@pytest.fixture
def priority(conn):
    p = _unique_priority()
    yield p
    conn.delete(results_stream_key(p))


def _forward_a_job(evidence_store: RedisMediaEvidenceStore, url: str, fingerprint_job_id: str) -> str:
    """Set up the crawler-side half of the picture: a discovered asset
    whose fingerprint job was claimed and forwarded -- the state a real
    bridge run leaves behind before any result exists."""
    aid = evidence_store.record_media_link(url=url, media_type="video", source_page="https://piracy.example/watch/1")
    job = evidence_store.claim_next_fingerprint_job("bridge-1")
    assert job is not None
    assert evidence_store.mark_fingerprint_job_forwarded(aid, job.token, fingerprint_job_id=fingerprint_job_id) is True
    return aid


def _consumer(evidence_store: RedisMediaEvidenceStore, priority: str, **overrides) -> FingerprintResultConsumer:
    kwargs = dict(consumer_group=f"test-group-{uuid.uuid4().hex}", block_ms=100, lease_ms=200, reclaim_batch_size=10)
    kwargs.update(overrides)
    return FingerprintResultConsumer(evidence_store, consumer_name="rc-1", priorities=(priority,), **kwargs)


class TestMatchResult:
    def test_match_result_is_recorded_and_correlated(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/movie.mp4", job_id)
        _write_result(
            conn, priority, job_id=job_id, media_evidence_id=aid, decision="match",
            confidence=0.9366, evidence='[{"technique": "dinov2", "matcher_version": "temporal_v1", "matched": true, "score": 0.9366}]',
        )

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.metrics.matches_recorded == 1

        asset = evidence_store.list_media_assets()[0]
        assert asset["status"] == "confirmed"
        assert asset["match_confidence"] == pytest.approx(0.9366)

        events = evidence_store.read_confirmed_match_events()
        assert len(events) == 1
        assert events[0]["asset_id"] == aid

    def test_evidence_result_contains_full_provenance_without_fingerprint_keys(self, evidence_store, conn, priority):
        """The success criterion: candidate URL, source domain, decision,
        confidence, and full match evidence must all be recoverable from
        evidence_store alone -- no fingerprint:* read required."""
        job_id = _unique_job_id()
        evidence_json = '[{"technique": "dinov2", "matched": true, "score": 0.9366, "detail": {"matched_segment_count": 4, "total_candidate_segments": 12}}]'
        aid = _forward_a_job(evidence_store, "https://pirate-site.example/movies/blast.mp4", job_id)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="match", confidence=0.9366, evidence=evidence_json)

        _consumer(evidence_store, priority).process_one()

        asset = evidence_store.list_media_assets()[0]
        assert asset["url"] == "https://pirate-site.example/movies/blast.mp4"
        # source_domain reflects the *source page* the media was found on
        # (_forward_a_job hardcodes https://piracy.example/watch/1), not the
        # CDN domain the media file itself is hosted on -- matches
        # record_media_link's actual, pre-existing semantics.
        assert asset["source_domain"] == "piracy.example"
        assert asset["status"] == "confirmed"
        assert asset["match_confidence"] == pytest.approx(0.9366)

        raw_result = evidence_store.redis_conn.hgetall(f"{evidence_store.namespace}:result:{aid}")
        assert raw_result["evidence"] == evidence_json


class TestDuplicateDelivery:
    def test_redelivered_result_is_a_safe_noop(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/movie.mp4", job_id)
        # Two distinct stream entries for the same job identity -- mirrors
        # the fingerprinter's own documented at-least-once redelivery case
        # (fingerprinter/tests/test_results.py::
        # test_duplicate_result_events_can_be_identified_safely).
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="match", confidence=0.9)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="match", confidence=0.9)

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.process_one() is True

        assert consumer.metrics.matches_recorded == 1
        assert consumer.metrics.stale_skipped == 1
        assert len(evidence_store.read_confirmed_match_events()) == 1


class TestNoMatchAndFailureResults:
    def test_no_match_is_persisted_without_confirmed_match_event(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/other.mp4", job_id)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="no_match", confidence=0.03)

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.metrics.no_matches_recorded == 1

        asset = evidence_store.list_media_assets()[0]
        assert asset["status"] == "rejected"
        assert evidence_store.read_confirmed_match_events() == []

    def test_processing_failure_is_persisted_as_uncertain(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/corrupt.mp4", job_id)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="processing_failure")

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.metrics.failures_recorded == 1

        asset = evidence_store.list_media_assets()[0]
        assert asset["status"] == "uncertain"
        assert evidence_store.read_confirmed_match_events() == []


class TestMismatchedAndMissingEvidence:
    def test_wrong_fingerprint_job_id_does_not_overwrite_evidence(self, evidence_store, conn, priority):
        forwarded_as = _unique_job_id()
        different_job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/movie.mp4", forwarded_as)
        # A result event correlated to a *different* job_id than the one
        # this asset was actually forwarded as -- must not complete it.
        _write_result(conn, priority, job_id=different_job_id, media_evidence_id=aid, decision="match")

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.metrics.stale_skipped == 1
        assert consumer.metrics.matches_recorded == 0

        asset = evidence_store.list_media_assets()[0]
        assert asset["status"] == "forwarded"  # untouched
        assert evidence_store.read_confirmed_match_events() == []

    def test_missing_crawler_evidence_is_handled_explicitly(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        _write_result(conn, priority, job_id=job_id, media_evidence_id="no-such-asset-id", decision="match")

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True  # does not raise/crash
        assert consumer.metrics.evidence_missing == 1
        assert consumer.metrics.matches_recorded == 0

        # acked, not left pending forever
        pending = conn.xpending(results_stream_key(priority), consumer._consumer_group)
        assert pending["pending"] == 0

    def test_malformed_stream_event_is_rejected_and_acked(self, evidence_store, conn, priority):
        conn.xadd(results_stream_key(priority), {"job_id": "x"})  # missing media_evidence_id/target_id/decision

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.metrics.malformed_rejected == 1

        pending = conn.xpending(results_stream_key(priority), consumer._consumer_group)
        assert pending["pending"] == 0

    def test_result_hash_missing_is_rejected_and_acked(self, evidence_store, conn, priority):
        """A well-formed event whose backing result hash doesn't exist
        (shouldn't happen given the fingerprinter's atomic commit, but must
        not crash the consumer if it ever does)."""
        job_id = _unique_job_id()
        conn.xadd(
            results_stream_key(priority),
            {"job_id": job_id, "media_evidence_id": "whatever", "target_id": "t", "decision": "match"},
        )

        consumer = _consumer(evidence_store, priority)
        assert consumer.process_one() is True
        assert consumer.metrics.malformed_rejected == 1


class TestCrashRecovery:
    def test_stale_pending_entry_is_recovered_via_reclaim(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/movie.mp4", job_id)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="match", confidence=0.9)

        group = f"test-group-{uuid.uuid4().hex}"
        # A consumer "claims" the entry (XREADGROUP) and then "crashes"
        # before acking -- entry sits in the PEL, unowned in effect. The
        # group must exist before a raw XREADGROUP can join it.
        conn.xgroup_create(results_stream_key(priority), group, id="0", mkstream=True)
        response = conn.xreadgroup(group, "crashed-consumer", {results_stream_key(priority): ">"}, count=1)
        assert response  # claimed, never acked

        time.sleep(0.15)  # past lease_ms

        recoverer = FingerprintResultConsumer(
            evidence_store, consumer_name="recoverer", priorities=(priority,),
            consumer_group=group, block_ms=100, lease_ms=50,
        )
        reclaimed = recoverer.reclaim_stale()
        assert reclaimed == 1
        assert recoverer.metrics.matches_recorded == 1

        asset = evidence_store.list_media_assets()[0]
        assert asset["status"] == "confirmed"
        assert len(evidence_store.read_confirmed_match_events()) == 1  # exactly once, not lost, not duplicated

        pending = conn.xpending(results_stream_key(priority), group)
        assert pending["pending"] == 0


class TestBlockingReadTimeoutRegression:
    """Regression coverage for a real, observed bug: `store.redis_conn`
    (storage/redis_media_evidence_store.py) is constructed with no explicit
    `socket_timeout`, which this environment's installed redis-py defaults
    to 5 seconds. A `block_ms=5000` XREADGROUP against an empty stream
    legitimately takes close to that long server-side -- sharing that
    connection for the blocking read races the client's 5s socket timeout
    against the server's own BLOCK timeout with ~0 margin, and loses:
    redis-py silently retries with growing backoff (observed: 25s, 41s,
    59s for three consecutive empty polls) before eventually raising
    `redis.exceptions.TimeoutError: Timeout reading from ...`. Fixed by
    giving the consumer its own dedicated connection
    (`_blocking_read_client`) with an adequately larger `socket_timeout`.
    This test fails loudly (by taking far longer than the assertion
    allows) if that fix is ever reverted or the connection is pointed back
    at `store.redis_conn`."""

    def test_empty_stream_poll_completes_near_block_ms_not_growing(self, evidence_store, conn, priority):
        consumer = _consumer(evidence_store, priority, block_ms=2000)
        for _ in range(3):
            started = time.monotonic()
            assert consumer.process_one() is False  # stream stays empty throughout
            elapsed = time.monotonic() - started
            assert elapsed < 4.0, (
                f"empty poll took {elapsed:.2f}s for block_ms=2000 -- expected ~2s; "
                "a value this much larger indicates the blocking-read connection's "
                "socket_timeout is once again too close to (or below) block_ms"
            )
        consumer.close()


class TestInfrastructureFailure:
    def test_infra_failure_does_not_ack(self, evidence_store, conn, priority):
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/movie.mp4", job_id)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="match")

        consumer = _consumer(evidence_store, priority)
        with patch.object(
            evidence_store, "complete_forwarded_fingerprint_job", side_effect=MediaEvidenceUnavailable("boom")
        ):
            with pytest.raises(MediaEvidenceUnavailable):
                consumer.process_one()

        # never acked -- still pending for a future retry/reclaim
        pending = conn.xpending(results_stream_key(priority), consumer._consumer_group)
        assert pending["pending"] == 1
        assert consumer.metrics.matches_recorded == 0
        assert consumer.metrics.stale_skipped == 0


class TestRuntimeLimit:
    """result_consumer_main.py's --runtime: a process-lifetime bound passed
    through to run_forever(deadline=...) as a time.monotonic() timestamp --
    never a per-result timeout (see run_forever's docstring)."""

    def test_run_forever_stops_immediately_when_deadline_already_elapsed(self, evidence_store, conn, priority):
        consumer = _consumer(evidence_store, priority)
        try:
            consumer.run_forever(deadline=time.monotonic() - 1.0)
            assert consumer._stop is True
        finally:
            consumer.close()

    def test_run_forever_keeps_running_until_its_own_deadline_elapses(self, evidence_store, conn, priority):
        """With an empty stream and no stop() call, run_forever would block
        on XREADGROUP forever (in block_ms chunks) without a deadline --
        this proves the deadline alone ends it, and that it doesn't hang."""
        consumer = _consumer(evidence_store, priority, block_ms=100)
        try:
            started = time.monotonic()
            consumer.run_forever(deadline=started + 0.2, idle_sleep_seconds=0.01)
            elapsed = time.monotonic() - started

            assert elapsed < 5.0
            assert consumer._stop is True
        finally:
            consumer.close()

    def test_run_forever_completes_an_already_available_result_before_its_deadline(
        self, evidence_store, conn, priority
    ):
        """A result already available when the deadline is still in the
        future is still consumed and recorded -- --runtime bounds process
        lifetime, it doesn't block work already within its window."""
        job_id = _unique_job_id()
        aid = _forward_a_job(evidence_store, "https://cdn.example/movie.mp4", job_id)
        _write_result(conn, priority, job_id=job_id, media_evidence_id=aid, decision="match", confidence=0.9)

        consumer = _consumer(evidence_store, priority)
        try:
            consumer.run_forever(deadline=time.monotonic() + 0.3, idle_sleep_seconds=0.01)
            assert consumer.metrics.matches_recorded == 1
        finally:
            consumer.close()
