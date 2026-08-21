"""Tests for bridge/crawler_fingerprinter_bridge.py -- the Phase 4
crawler->fingerprinter bridge orchestration (docs/architecture/
phase-4-crawler-fingerprinter-bridge.md). Run against real local Redis
(test DB 1, this repo's existing Redis-test convention) -- skip cleanly if
unavailable. Never touches production DB 0.

Covers the brief's STEP 4 checklist: valid forwarding, target/priority/
media-field propagation, malformed/missing/unregistered-target rejection,
destination submission failure, ack-only-after-destination-durability,
crash/reclaim recovery, duplicate forwarding, Redis-unavailable handling,
graceful shutdown, and restart recovery.
"""
from __future__ import annotations

import uuid

import pytest
import redis

from bridge.crawler_fingerprinter_bridge import BridgeRuntimeConfig, CrawlerFingerprinterBridge
from bridge.fingerprint_stream_adapter import stream_key
from core.target_scope import TargetScope
from storage.media_evidence_store import MediaEvidenceUnavailable
from storage.redis_media_evidence_store import RedisMediaEvidenceStore

_ALL_PRIORITY_STREAMS = ("high", "default", "low")
_TEST_TARGET_PREFIX = "bridge_test"


def _force_expire_lease(store: RedisMediaEvidenceStore, asset_id: str) -> None:
    """Test hook: backdate a job's inflight lease score into the past,
    simulating an abandoned/crashed bridge process without waiting out the
    lease (same technique as tests/redis_media_evidence_store_test.py)."""
    store.redis_conn.zadd(store._key("jobs", "inflight"), {asset_id: 0})


def _register_target(conn: "redis.Redis", target_id: str, target_version: str) -> None:
    """Seed the fingerprinter's own `fingerprint:target:*` key directly
    (same technique as tests/target_scope_test.py) -- the bridge only ever
    reads this via `verify_target_registered`'s read-only EXISTS check, so
    seeding it this way (rather than invoking the real TargetRegistry,
    which would require the fingerprinter's environment) is faithful to
    what the bridge actually observes."""
    conn.hset(f"fingerprint:target:{target_id}:{target_version}", mapping={"content_sha256": "deadbeef"})


def _unique_target_id() -> str:
    return f"{_TEST_TARGET_PREFIX}:{uuid.uuid4().hex}"


@pytest.fixture
def scoped_store():
    """A target-scoped RedisMediaEvidenceStore plus the matching registered
    target -- the common case the bridge exists to serve."""
    try:
        conn = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        conn.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")

    target_id = _unique_target_id()
    target_version = "v1"
    _register_target(conn, target_id, target_version)

    store = RedisMediaEvidenceStore(
        redis_host="localhost",
        redis_port=6379,
        redis_db=1,
        namespace="test_bridge_evidence",
        fingerprint_lease_ttl=5.0,
        max_retries=2,
        base_backoff=0.0,
        max_backoff=0.0,
        target_scope=TargetScope(target_id=target_id, target_version=target_version),
    )
    store.clear()
    for priority in _ALL_PRIORITY_STREAMS:
        conn.delete(stream_key(priority))

    yield store, target_id, target_version

    store.clear()
    store.close()
    for priority in _ALL_PRIORITY_STREAMS:
        conn.delete(stream_key(priority))
    for key in conn.keys(f"fingerprint:target:{_TEST_TARGET_PREFIX}:*"):
        conn.delete(key)
    for key in conn.keys("fingerprint:submission:*"):
        conn.delete(key)
    conn.close()


@pytest.fixture
def unscoped_store():
    try:
        conn = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        conn.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")

    store = RedisMediaEvidenceStore(
        redis_host="localhost",
        redis_port=6379,
        redis_db=1,
        namespace="test_bridge_evidence",
        fingerprint_lease_ttl=5.0,
        max_retries=2,
        base_backoff=0.0,
        max_backoff=0.0,
    )
    store.clear()
    for priority in _ALL_PRIORITY_STREAMS:
        conn.delete(stream_key(priority))

    yield store

    store.clear()
    store.close()
    for priority in _ALL_PRIORITY_STREAMS:
        conn.delete(stream_key(priority))
    for key in conn.keys("fingerprint:submission:*"):
        conn.delete(key)
    conn.close()


def _bridge(store, **overrides) -> CrawlerFingerprinterBridge:
    config = BridgeRuntimeConfig(
        max_outstanding_jobs=overrides.pop("max_outstanding_jobs", 500),
        submission_marker_ttl_s=3600,
        priority_high_max=5,
        priority_low_min=15,
        idle_sleep_seconds=0.01,
        reclaim_interval_seconds=0.0,
        **overrides,
    )
    return CrawlerFingerprinterBridge(store, worker_id="bridge-test", config=config)


class TestValidForwarding:
    def test_valid_job_is_forwarded_with_all_fields_intact(self, scoped_store):
        store, target_id, target_version = scoped_store
        aid = store.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/1",
            media_type="video",
            priority=10,
        )
        bridge = _bridge(store)

        assert bridge.process_one() is True

        entries = store.redis_conn.xrange(stream_key("default"))
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["media_url"] == "https://cdn.example/movie.mp4"
        assert fields["media_evidence_id"] == aid
        assert fields["media_type"] == "video"
        assert fields["source_domain"] == "piracy.example"
        assert fields["target_id"] == target_id
        assert fields["target_version"] == target_version
        assert fields["techniques"] == "dinov2"
        assert fields["max_attempts"] == "3"

        jobs = store.get_fingerprint_jobs()
        assert jobs == []  # forwarded jobs have no index, mirroring `completed`
        counts = store.get_status_counts()
        assert counts["forwarded"] == 1
        assert counts["claimed"] == 0

        assert bridge.metrics.jobs_claimed == 1
        assert bridge.metrics.jobs_submitted == 1
        assert bridge.metrics.jobs_acknowledged == 1

    def test_empty_queue_returns_false_without_error(self, scoped_store):
        store, _, _ = scoped_store
        bridge = _bridge(store)
        assert bridge.process_one() is False


class TestPriorityPropagation:
    @pytest.mark.parametrize(
        "crawler_priority,expected_stream",
        [(1, "high"), (5, "high"), (6, "default"), (10, "default"), (14, "default"), (15, "low"), (30, "low")],
    )
    def test_crawler_priority_maps_to_the_documented_band(self, scoped_store, crawler_priority, expected_stream):
        store, _, _ = scoped_store
        store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=crawler_priority)
        bridge = _bridge(store)

        assert bridge.process_one() is True

        assert store.redis_conn.xlen(stream_key(expected_stream)) == 1
        for other in _ALL_PRIORITY_STREAMS:
            if other != expected_stream:
                assert store.redis_conn.xlen(stream_key(other)) == 0


class TestTargetPropagation:
    def test_missing_target_scope_is_permanently_rejected_without_fabrication(self, unscoped_store):
        store = unscoped_store
        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        bridge = _bridge(store)

        assert bridge.process_one() is True

        assert store.redis_conn.xlen(stream_key("default")) == 0  # never forwarded
        jobs = store.get_fingerprint_jobs(statuses=["permanent_failure"])
        assert len(jobs) == 1
        assert jobs[0]["asset_id"] == aid
        assert jobs[0]["error_class"] == "missing_target_scope"
        assert bridge.metrics.jobs_rejected == 1

    def test_unregistered_target_is_permanently_rejected(self, scoped_store):
        """The target scope on the job is well-formed but was never
        registered in the fingerprinter's TargetRegistry (simulated by
        never seeding its `fingerprint:target:*` key) -- the bridge must
        not burn a fingerprint worker's cycle on a job guaranteed to fail."""
        store, target_id, target_version = scoped_store
        # Delete the registration this fixture seeded, so the scope on the
        # job (fixed at record_media_link time) now points at nothing.
        store.redis_conn.delete(f"fingerprint:target:{target_id}:{target_version}")

        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        bridge = _bridge(store)

        assert bridge.process_one() is True

        assert store.redis_conn.xlen(stream_key("default")) == 0
        jobs = store.get_fingerprint_jobs(statuses=["permanent_failure"])
        assert len(jobs) == 1
        assert jobs[0]["asset_id"] == aid
        assert jobs[0]["error_class"] == "target_not_registered"


class TestMalformedCandidate:
    def test_asset_with_no_resolvable_source_domain_is_permanently_rejected(self, scoped_store):
        """Defensive path: the evidence store always writes some
        source_domain in practice (Phase 3 doc §5's field mapping), but a
        corrupted/legacy hash must still be handled without an infinite
        retry loop -- mirrors the fingerprinter's own
        `FingerprintCandidate.validate()` requiring a non-empty value."""
        store, _, _ = scoped_store
        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        store.redis_conn.hset(f"{store.namespace}:asset:{aid}", "source_domain", "")

        bridge = _bridge(store)
        assert bridge.process_one() is True

        assert store.redis_conn.xlen(stream_key("default")) == 0
        jobs = store.get_fingerprint_jobs(statuses=["permanent_failure"])
        assert len(jobs) == 1
        assert jobs[0]["error_class"] == "invalid_candidate"


class TestBackpressure:
    def test_backpressure_rejection_is_retryable_not_lost(self, scoped_store):
        store, _, _ = scoped_store
        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        bridge = _bridge(store, max_outstanding_jobs=0)

        assert bridge.process_one() is True

        assert store.redis_conn.xlen(stream_key("default")) == 0
        retry_jobs = store.get_fingerprint_jobs(statuses=["retry_scheduled"])
        assert len(retry_jobs) == 1
        assert retry_jobs[0]["asset_id"] == aid
        assert retry_jobs[0]["error_class"] == "backpressure"
        assert bridge.metrics.jobs_retried == 1

        # Once the backlog clears, the retried job is claimable again and
        # forwards successfully -- nothing was permanently lost.
        store.redis_conn.zadd(store._key("jobs", "retry_scheduled"), {aid: 0})
        reclaimed, requeued = store.reclaim_expired_jobs()
        assert requeued == 1

        bridge2 = _bridge(store, max_outstanding_jobs=500)
        assert bridge2.process_one() is True
        assert store.redis_conn.xlen(stream_key("default")) == 1


class TestAckOrdering:
    def test_source_job_stays_claimed_and_recoverable_if_destination_write_fails(self, scoped_store, monkeypatch):
        """Rule 7: the source job must never be acknowledged/removed merely
        because the bridge parsed it -- only after a durable destination
        write. Simulate a destination-side Redis failure during XADD and
        assert the source job is left exactly as a genuine mid-flight crash
        would leave it: still `claimed`, recoverable via the existing
        lease/reclaim mechanism, never silently dropped and never falsely
        marked forwarded or failed."""
        store, _, _ = scoped_store
        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        real_xadd = store.redis_conn.xadd

        def _failing_xadd(*args, **kwargs):
            raise redis.exceptions.ConnectionError("simulated destination Redis failure")

        monkeypatch.setattr(store.redis_conn, "xadd", _failing_xadd)
        bridge = _bridge(store)

        with pytest.raises(redis.exceptions.ConnectionError):
            bridge.process_one()

        monkeypatch.setattr(store.redis_conn, "xadd", real_xadd)

        # Not forwarded, not permanently failed, not retry-scheduled --
        # still claimed, exactly where a real crash would have left it.
        counts = store.get_status_counts()
        assert counts["claimed"] == 1
        assert counts["forwarded"] == 0
        assert counts["permanent_failure"] == 0
        assert store.redis_conn.xlen(stream_key("default")) == 0

        # And it is recoverable: once the lease expires, reclaim + a fresh
        # claim can still forward it successfully.
        _force_expire_lease(store, aid)
        store.reclaim_expired_jobs()
        store.reclaim_expired_jobs()

        bridge2 = _bridge(store)
        assert bridge2.process_one() is True
        assert store.redis_conn.xlen(stream_key("default")) == 1
        assert store.get_status_counts()["forwarded"] == 1


class TestCrashAndDuplicateRecovery:
    def test_crash_after_xadd_before_ack_is_recovered_without_duplicating_the_stream_entry(self, scoped_store):
        """Case E from the brief's crash-window list: the bridge crashes
        after a successful XADD but before acknowledging the source job.
        A second bridge instance reclaiming the abandoned claim must
        re-derive the same deterministic job_id, get DUPLICATE_SUPPRESSED
        from the destination's own dedup marker, and still correctly mark
        the source job forwarded -- never a second stream entry."""
        store, target_id, target_version = scoped_store
        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        from bridge.fingerprint_stream_adapter import BridgePriority, submit_job

        job = store.claim_next_fingerprint_job(worker_id="crashed-bridge")
        assert job is not None
        result = submit_job(
            store.redis_conn,
            candidate_url=job.canonical_url,
            media_evidence_id=job.asset_id,
            media_type=job.media_type,
            source_domain="cdn.example",
            target_id=target_id,
            target_version=target_version,
            priority=BridgePriority.NORMAL,
            max_attempts=3,
            max_outstanding_jobs=500,
            submission_marker_ttl_s=3600,
        )
        assert result.outcome.value == "enqueued"
        assert store.redis_conn.xlen(stream_key("default")) == 1
        # Simulate the crash: never call mark_fingerprint_job_forwarded.
        # Instead, the claim's lease eventually expires and gets reclaimed.
        _force_expire_lease(store, aid)
        store.reclaim_expired_jobs()
        store.reclaim_expired_jobs()

        bridge = _bridge(store)
        assert bridge.process_one() is True

        # Still exactly one stream entry -- the retry hit DUPLICATE_SUPPRESSED.
        assert store.redis_conn.xlen(stream_key("default")) == 1
        assert bridge.metrics.jobs_duplicated == 1
        assert store.get_status_counts()["forwarded"] == 1


class TestInfrastructureFailure:
    def test_claim_time_redis_unavailable_propagates_not_swallowed(self, scoped_store, monkeypatch):
        store, _, _ = scoped_store
        bridge = _bridge(store)

        def _raise(*args, **kwargs):
            raise redis.exceptions.ConnectionError("simulated source Redis outage")

        monkeypatch.setattr(store, "claim_next_fingerprint_job", _raise)

        with pytest.raises(redis.exceptions.ConnectionError):
            bridge.process_one()

    def test_run_forever_backs_off_on_infrastructure_failure_and_still_stops(self, scoped_store, monkeypatch):
        store, _, _ = scoped_store
        bridge = _bridge(store)

        call_count = {"n": 0}

        def _raise_once_then_stop(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                bridge.stop()
            raise MediaEvidenceUnavailable("simulated")

        monkeypatch.setattr(store, "claim_next_fingerprint_job", _raise_once_then_stop)
        bridge.run_forever()  # must return, not raise or hang
        assert call_count["n"] >= 2


class TestGracefulShutdown:
    def test_stop_halts_run_forever_promptly(self, scoped_store):
        store, _, _ = scoped_store
        bridge = _bridge(store)
        bridge.stop()
        bridge.run_forever()  # already stopped -- must return immediately


class TestRestartRecovery:
    def test_a_job_abandoned_by_one_bridge_instance_is_forwarded_by_a_fresh_one(self, scoped_store):
        store, target_id, target_version = scoped_store
        aid = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        # First "process" (e.g. a bridge instance that crashed right after
        # claiming, before ever reaching submission) just claims and dies.
        crashed_job = store.claim_next_fingerprint_job(worker_id="bridge-instance-1")
        assert crashed_job is not None
        _force_expire_lease(store, aid)
        store.reclaim_expired_jobs()
        store.reclaim_expired_jobs()

        # A freshly-started bridge process picks the job back up.
        fresh_bridge = _bridge(store)
        assert fresh_bridge.process_one() is True

        entries = store.redis_conn.xrange(stream_key("default"))
        assert len(entries) == 1
        assert entries[0][1]["target_id"] == target_id
        assert entries[0][1]["target_version"] == target_version
        assert store.get_status_counts()["forwarded"] == 1
