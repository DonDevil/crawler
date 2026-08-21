"""Tests for the Redis media evidence store (storage/redis_media_evidence_store.py).

Covers asset dedup/rediscovery, bounded observation/variant retention, the
fingerprint job claim/lease/CAS/retry machinery, and the confirmed_match
event -- the Redis-specific behaviors documented in
docs/architecture/media-evidence-redis-design.md and
docs/architecture/media-evidence-step1.md.

Run this test ONLY if Redis is available locally on port 6379. True
multi-process claim-safety coverage (independent OS processes, not just
threads) lives in tests/media_evidence_multiprocess_test.py.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from core.target_scope import TargetScope
from storage.media_evidence_store import FingerprintResult, JOB_PERMANENT_FAILURE, JOB_QUEUED, JOB_RETRY_SCHEDULED
from storage.redis_media_evidence_store import RedisMediaEvidenceStore


@pytest.fixture
def evidence_store() -> RedisMediaEvidenceStore:
    """Create a Redis media evidence store and clear it for testing."""
    try:
        store = RedisMediaEvidenceStore(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,  # Same test DB as tests/redis_frontier_test.py -- different namespace, no collision
            namespace="test_evidence",
            fingerprint_lease_ttl=5.0,
            max_retries=2,
            base_backoff=0.0,
            max_backoff=0.0,
        )
        store.clear()
        yield store
        store.clear()
        store.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


def _force_expire_lease(store: RedisMediaEvidenceStore, asset_id: str) -> None:
    """Test hook: backdate a job's inflight lease score into the past,
    simulating an abandoned/crashed worker without waiting out the lease."""
    store.redis_conn.zadd(store._key("jobs", "inflight"), {asset_id: 0})


class TestAssetAndObservationBehavior:
    def test_duplicate_discovery_returns_same_asset_and_one_job(self, evidence_store: RedisMediaEvidenceStore):
        aid1 = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=10)
        aid2 = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=10)

        assert aid1 == aid2
        assert len(evidence_store.list_media_assets()) == 1
        assert len(evidence_store.get_fingerprint_jobs()) == 1

    def test_rediscovery_only_ratchets_priority_down(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=10)
        evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=20)
        jobs = evidence_store.get_fingerprint_jobs()
        assert jobs[0]["priority"] == 10  # higher (less urgent) priority ignored

        evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=3)
        jobs = evidence_store.get_fingerprint_jobs()
        assert jobs[0]["priority"] == 3

    def test_observation_count_is_exact_but_history_is_capped(self, evidence_store: RedisMediaEvidenceStore):
        evidence_store.max_observations_per_asset = 3
        aid = None
        for i in range(5):
            aid = evidence_store.record_media_link(
                url="https://cdn.example/movie.mp4",
                media_type="video",
                discovered_by=f"crawler-{i}",
            )

        assets = evidence_store.list_media_assets()
        assert assets[0]["observation_count"] == 5

        observations = evidence_store.list_observations(aid)
        assert len(observations) == 3
        # LPUSH+LTRIM keeps the newest entries at the head.
        discovered_by_seen = [o["discovered_by"] for o in observations]
        assert discovered_by_seen == ["crawler-4", "crawler-3", "crawler-2"]

    def test_rediscovering_a_claimed_job_does_not_disturb_its_status(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=1)

        jobs = evidence_store.get_fingerprint_jobs(statuses=["claimed"])
        assert len(jobs) == 1
        assert jobs[0]["asset_id"] == aid

    def test_manifest_variants_are_capped(self, evidence_store: RedisMediaEvidenceStore):
        evidence_store.max_variants_per_asset = 2
        aid = evidence_store.record_media_link(
            url="https://cdn.example/master.m3u8", media_type="stream-manifest"
        )
        evidence_store.record_manifest_variants(
            aid,
            [
                {"url": "https://cdn.example/v1.m3u8", "bandwidth": 100},
                {"url": "https://cdn.example/v2.m3u8", "bandwidth": 200},
                {"url": "https://cdn.example/v3.m3u8", "bandwidth": 300},
            ],
        )
        variants = evidence_store.list_manifest_variants(aid)
        assert len(variants) == 2

        # Updating an already-known variant is still allowed past the cap.
        evidence_store.record_manifest_variants(aid, [{"url": "https://cdn.example/v1.m3u8", "bandwidth": 150}])
        variants = evidence_store.list_manifest_variants(aid)
        assert len(variants) == 2
        assert {v["bandwidth"] for v in variants} == {150, 200}


class TestClaimAndCAS:
    def test_claim_returns_none_when_queue_empty(self, evidence_store: RedisMediaEvidenceStore):
        assert evidence_store.claim_next_fingerprint_job("worker-1") is None

    def test_concurrent_claims_never_duplicate(self, evidence_store: RedisMediaEvidenceStore):
        job_count = 30
        for i in range(job_count):
            evidence_store.record_media_link(url=f"https://cdn.example/movie-{i}.mp4", media_type="video")

        claimed_ids: list[str] = []

        def _claim(worker_id: str):
            store = RedisMediaEvidenceStore(
                redis_host="localhost", redis_port=6379, redis_db=1, namespace="test_evidence"
            )
            try:
                job = store.claim_next_fingerprint_job(worker_id)
                return job.asset_id if job else None
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(_claim, [f"worker-{i}" for i in range(job_count)]))

        claimed_ids = [r for r in results if r is not None]
        assert len(claimed_ids) == job_count
        assert len(set(claimed_ids)) == job_count  # no duplicate claims

    def test_wrong_token_cannot_renew_or_complete(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        assert evidence_store.renew_job_lease(aid, "not-the-token") is False
        assert evidence_store.renew_job_lease(aid, job.token) is True

        assert evidence_store.complete_fingerprint_job(
            aid, "not-the-token", result=FingerprintResult(aggregate_decision="confirmed")
        ) is False
        assert evidence_store.complete_fingerprint_job(
            aid, job.token, result=FingerprintResult(aggregate_decision="confirmed")
        ) is True

    def test_stale_worker_cannot_overwrite_newer_result(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        stale_job = evidence_store.claim_next_fingerprint_job("worker-a")
        assert stale_job is not None

        _force_expire_lease(evidence_store, aid)
        # Both reclaim phases share one Redis TIME snapshot per call, and
        # ZRANGEBYSCORE's range is inclusive, so with base_backoff=0 (this
        # fixture) the just-rescheduled retry is immediately due and
        # promoted back to queued within this same call.
        reclaimed, requeued = evidence_store.reclaim_expired_jobs()
        assert reclaimed == 1
        assert requeued == 1

        new_job = evidence_store.claim_next_fingerprint_job("worker-b")
        assert new_job is not None
        assert new_job.token != stale_job.token

        assert evidence_store.complete_fingerprint_job(
            aid, stale_job.token, result=FingerprintResult(aggregate_decision="confirmed", matched_title="wrong")
        ) is False

        assert evidence_store.complete_fingerprint_job(
            aid, new_job.token, result=FingerprintResult(aggregate_decision="confirmed", matched_title="right")
        ) is True

        assets = evidence_store.list_media_assets()
        assert assets[0]["matched_title"] == "right"


class TestLeaseAndRecovery:
    def test_unexpired_claim_is_not_reclaimed(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        evidence_store.claim_next_fingerprint_job("worker-1")

        reclaimed, requeued = evidence_store.reclaim_expired_jobs()
        assert reclaimed == 0
        assert requeued == 0

    def test_expired_claim_is_reclaimed_and_requeued(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        evidence_store.claim_next_fingerprint_job("worker-1")

        _force_expire_lease(evidence_store, aid)
        reclaimed, requeued = evidence_store.reclaim_expired_jobs()
        assert reclaimed == 1
        assert requeued == 1

        jobs = evidence_store.get_fingerprint_jobs(statuses=[JOB_QUEUED])
        assert len(jobs) == 1
        assert jobs[0]["retry_count"] == 1

    def test_repeated_crashes_exhaust_retries_into_permanent_failure(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        for _ in range(evidence_store.max_retries):
            job = evidence_store.claim_next_fingerprint_job("worker")
            assert job is not None, "job should still be claimable before retries are exhausted"
            _force_expire_lease(evidence_store, aid)
            evidence_store.reclaim_expired_jobs()
            evidence_store.reclaim_expired_jobs()

        jobs = evidence_store.get_fingerprint_jobs(statuses=[JOB_PERMANENT_FAILURE])
        assert len(jobs) == 1
        assert jobs[0]["asset_id"] == aid


class TestFailAndRetry:
    def test_retryable_failure_is_reclaimable_and_requeued(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        assert evidence_store.fail_fingerprint_job(
            aid, job.token, error_class="network", last_error="timeout", retryable=True
        ) is True

        jobs = evidence_store.get_fingerprint_jobs(statuses=[JOB_RETRY_SCHEDULED])
        assert len(jobs) == 1
        assert jobs[0]["retry_count"] == 1

    def test_non_retryable_failure_is_immediately_permanent(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        assert evidence_store.fail_fingerprint_job(
            aid, job.token, error_class="corrupt_media", last_error="cannot decode", retryable=False
        ) is True

        jobs = evidence_store.get_fingerprint_jobs(statuses=[JOB_PERMANENT_FAILURE])
        assert len(jobs) == 1

    def test_stale_token_cannot_fail_a_reclaimed_job(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        stale_job = evidence_store.claim_next_fingerprint_job("worker-a")
        assert stale_job is not None

        _force_expire_lease(evidence_store, aid)
        evidence_store.reclaim_expired_jobs()
        evidence_store.reclaim_expired_jobs()

        new_job = evidence_store.claim_next_fingerprint_job("worker-b")
        assert new_job is not None

        assert evidence_store.fail_fingerprint_job(
            aid, stale_job.token, error_class="network", last_error="stale", retryable=True
        ) is False


class TestMarkForwarded:
    """`mark_fingerprint_job_forwarded` (docs/architecture/
    phase-4-crawler-fingerprinter-bridge.md) -- the bridge's terminal
    transition for a job it successfully handed off to the fingerprinter,
    distinct from `complete_fingerprint_job` (which records an actual
    verdict the bridge never has)."""

    def test_forwarded_job_leaves_claimed_state_and_records_job_id(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        assert evidence_store.mark_fingerprint_job_forwarded(
            aid, job.token, fingerprint_job_id="deadbeef" * 4
        ) is True

        counts = evidence_store.get_status_counts()
        assert counts["claimed"] == 0
        assert counts["forwarded"] == 1

        jobs = evidence_store.get_fingerprint_jobs()
        assert jobs == []  # no index for `forwarded`, mirroring `completed`'s precedent

    def test_forwarded_status_is_not_reported_as_a_fingerprint_verdict(self, evidence_store: RedisMediaEvidenceStore):
        """A forwarded-but-not-yet-fingerprinted asset must never look like
        a confirmed/rejected/uncertain verdict to a reader of
        `list_media_assets()` -- fabricating a decision would be worse than
        not forwarding at all (brief, rule 12)."""
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None
        evidence_store.mark_fingerprint_job_forwarded(aid, job.token, fingerprint_job_id="abc123")

        asset = evidence_store.list_media_assets()[0]
        assert asset["status"] == "forwarded"
        assert asset["status"] not in ("confirmed", "rejected", "uncertain")
        assert asset["match_confidence"] is None
        assert asset["matched_title"] is None

    def test_wrong_token_cannot_mark_forwarded(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        assert evidence_store.mark_fingerprint_job_forwarded(aid, "not-the-token", fingerprint_job_id="x") is False

        counts = evidence_store.get_status_counts()
        assert counts["claimed"] == 1
        assert counts["forwarded"] == 0

    def test_stale_claim_cannot_be_marked_forwarded_after_reclaim(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        stale_job = evidence_store.claim_next_fingerprint_job("worker-a")
        assert stale_job is not None

        _force_expire_lease(evidence_store, aid)
        evidence_store.reclaim_expired_jobs()
        evidence_store.reclaim_expired_jobs()

        assert evidence_store.mark_fingerprint_job_forwarded(
            aid, stale_job.token, fingerprint_job_id="x"
        ) is False


class TestConfirmedMatchEvent:
    def test_confirmed_decision_emits_event(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/1",
            media_type="video",
        )
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        evidence_store.complete_fingerprint_job(
            aid, job.token, result=FingerprintResult(aggregate_decision="confirmed", matched_title="Movie", confidence=0.9)
        )

        events = evidence_store.read_confirmed_match_events()
        assert len(events) == 1
        assert events[0]["asset_id"] == aid
        assert events[0]["source_domain"] == "piracy.example"
        assert events[0]["matched_title"] == "Movie"

    def test_rejected_decision_does_not_emit_event(self, evidence_store: RedisMediaEvidenceStore):
        aid = evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = evidence_store.claim_next_fingerprint_job("worker-1")
        assert job is not None

        evidence_store.complete_fingerprint_job(aid, job.token, result=FingerprintResult(aggregate_decision="rejected"))

        assert evidence_store.read_confirmed_match_events() == []


class TestStatusCounts:
    def test_status_counts_reflect_job_lifecycle(self, evidence_store: RedisMediaEvidenceStore):
        aid1 = evidence_store.record_media_link(url="https://cdn.example/a.mp4", media_type="video")
        evidence_store.record_media_link(url="https://cdn.example/b.mp4", media_type="video")

        counts = evidence_store.get_status_counts()
        assert counts[JOB_QUEUED] == 2

        job = evidence_store.claim_next_fingerprint_job("worker-1")
        counts = evidence_store.get_status_counts()
        assert counts[JOB_QUEUED] == 1
        assert counts["claimed"] == 1

        evidence_store.complete_fingerprint_job(job.asset_id, job.token, result=FingerprintResult(aggregate_decision="uncertain"))
        counts = evidence_store.get_status_counts()
        assert counts["completed"] == 1
        assert counts["claimed"] == 0


@pytest.fixture
def scoped_evidence_store() -> RedisMediaEvidenceStore:
    """Same isolated test keyspace as `evidence_store`, but constructed with
    an explicit target scope (docs/architecture/
    phase-3-target-registration-and-scoping.md) -- covers what a
    target-scoped crawler run does differently."""
    try:
        store = RedisMediaEvidenceStore(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_evidence",
            fingerprint_lease_ttl=5.0,
            max_retries=2,
            base_backoff=0.0,
            max_backoff=0.0,
            target_scope=TargetScope(target_id="test-target", target_version="v1"),
        )
        store.clear()
        yield store
        store.clear()
        store.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


class TestTargetScopeAssociation:
    """Covers Phase 3's requirement that "target identity survives through
    the relevant crawler/evidence path that the future bridge will
    consume" -- via `claim_next_fingerprint_job`, the one place a future
    bridge would actually read it from."""

    def test_scoped_run_associates_target_with_new_jobs(self, scoped_evidence_store: RedisMediaEvidenceStore):
        scoped_evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        job = scoped_evidence_store.claim_next_fingerprint_job("worker-1")

        assert job is not None
        assert job.target_id == "test-target"
        assert job.target_version == "v1"

    def test_target_association_appears_in_job_introspection(self, scoped_evidence_store: RedisMediaEvidenceStore):
        scoped_evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        jobs = scoped_evidence_store.get_fingerprint_jobs()

        assert len(jobs) == 1
        assert jobs[0]["target_id"] == "test-target"
        assert jobs[0]["target_version"] == "v1"

    def test_unscoped_run_creates_jobs_with_no_target_association(self, evidence_store: RedisMediaEvidenceStore):
        """Existing (pre-Phase-3) behavior is unchanged when no target
        scope is configured -- no implicit/default target is ever
        substituted (brief: "Do not create an implicit default target")."""
        evidence_store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        job = evidence_store.claim_next_fingerprint_job("worker-1")

        assert job is not None
        assert job.target_id is None
        assert job.target_version is None

    def test_target_is_never_inferred_from_discovery_metadata(self, scoped_evidence_store: RedisMediaEvidenceStore):
        """A job's target association comes only from the store's own
        configured scope, never from the URL, source page, or discovery
        method a candidate happens to carry -- discovery metadata that
        looks like a movie title must not leak into target identity
        (brief: "Do not infer target scope from search queries" /
        "filenames or seed URLs")."""
        scoped_evidence_store.record_media_link(
            url="https://cdn.example/Blast.Full.Movie.Download.1080p.mp4",
            source_page="https://example.com/search?q=Blast+full+movie+download",
            discovered_by="search-query",
            media_type="video",
        )

        job = scoped_evidence_store.claim_next_fingerprint_job("worker-1")

        # Still the store's configured scope ("test-target"/"v1"), not
        # anything derived from the URL/query text above.
        assert job.target_id == "test-target"
        assert job.target_version == "v1"
