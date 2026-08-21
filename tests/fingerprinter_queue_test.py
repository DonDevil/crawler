"""Tests for the fingerprint job queue: claim/complete/fail lifecycle,
token CAS, and the evidence/DomainDatabase decoupling (§19)."""

from storage.domain_database import DomainDatabase
from storage.media_evidence_store import FingerprintResult, JOB_PERMANENT_FAILURE, JOB_QUEUED, JOB_RETRY_SCHEDULED
from storage.sqlite_media_evidence_store import SQLiteMediaEvidenceStore


def test_fingerprinter_can_claim_and_complete_a_job(tmp_path):
    store = SQLiteMediaEvidenceStore(path=str(tmp_path / "media_evidence.db"))

    try:
        asset_id = store.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/123",
            discovered_by="async",
            discovery_method="anchor",
            media_type="video",
            priority=3,
        )

        job = store.claim_next_fingerprint_job(worker_id="fingerprinter-1")
        assert job is not None
        assert job.asset_id == asset_id

        # Queue is now empty -- a second claimer gets nothing.
        assert store.claim_next_fingerprint_job(worker_id="fingerprinter-2") is None

        completed = store.complete_fingerprint_job(
            asset_id,
            job.token,
            result=FingerprintResult(aggregate_decision="confirmed", confidence=0.97, matched_title="Target Movie"),
        )
        assert completed is True

        asset = store.list_media_assets()[0]
        assert asset["status"] == "confirmed"
        assert asset["match_confidence"] == 0.97
        assert asset["matched_title"] == "Target Movie"
    finally:
        store.close()


def test_completing_a_job_does_not_touch_domain_database(tmp_path):
    """Architecture decision (docs/architecture/media-evidence-redis-design.md
    §19): the evidence layer never talks to `DomainDatabase` directly.
    Confirming a match only writes evidence -- domain-score feedback is a
    future, decoupled consumer of the confirmed_match event, not part of
    this store."""
    media_store = SQLiteMediaEvidenceStore(path=str(tmp_path / "media_evidence.db"))
    domain_db = DomainDatabase(path=str(tmp_path / "domains.db"))

    try:
        asset_id = media_store.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/123",
            referrer_url="https://piracy.example/",
            discovered_by="playwright",
            discovery_method="network-response",
            media_type="video",
            priority=2,
        )

        job = media_store.claim_next_fingerprint_job(worker_id="fingerprinter-1")
        assert job is not None

        media_store.complete_fingerprint_job(
            asset_id,
            job.token,
            result=FingerprintResult(aggregate_decision="confirmed", confidence=0.99, matched_title="Target Movie"),
        )

        asset = media_store.list_media_assets()[0]
        assert asset["status"] == "confirmed"
        assert asset["source_domain"] == "piracy.example"

        # No direct coupling: completing a job never wrote to DomainDatabase.
        assert domain_db.get_score("piracy.example") is None
    finally:
        media_store.close()
        domain_db.close()


def test_stale_token_cannot_complete_a_reclaimed_job(tmp_path):
    store = SQLiteMediaEvidenceStore(path=str(tmp_path / "media_evidence.db"), max_retries=5)

    try:
        asset_id = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        stale_job = store.claim_next_fingerprint_job(worker_id="worker-a")
        assert stale_job is not None

        # Simulate worker-a crashing: force its lease into the past, then
        # let the recovery sweep reclaim it.
        store._writer.execute(
            "UPDATE fingerprint_jobs SET lease_expires_at = 0 WHERE asset_id = ?", (int(asset_id),)
        )
        store._writer.flush()
        reclaimed, requeued = store.reclaim_expired_jobs()
        assert reclaimed == 1

        # The reclaimed job is now retry_scheduled with a future backoff;
        # force it due and sweep again to promote it back to queued.
        store._writer.execute(
            "UPDATE fingerprint_jobs SET retry_eligible_at = 0 WHERE asset_id = ?", (int(asset_id),)
        )
        store._writer.flush()
        _, requeued = store.reclaim_expired_jobs()
        assert requeued == 1

        new_job = store.claim_next_fingerprint_job(worker_id="worker-b")
        assert new_job is not None
        assert new_job.token != stale_job.token

        # worker-a's stale token must be rejected, and must not disturb
        # worker-b's now-current claim.
        assert store.complete_fingerprint_job(
            asset_id, stale_job.token, result=FingerprintResult(aggregate_decision="confirmed")
        ) is False

        jobs = store.get_fingerprint_jobs()
        assert jobs[0]["status"] == "claimed"
        assert jobs[0]["claim_token"] == new_job.token

        assert store.complete_fingerprint_job(
            asset_id, new_job.token, result=FingerprintResult(aggregate_decision="confirmed")
        ) is True
    finally:
        store.close()


def test_mark_fingerprint_job_forwarded_sqlite_parity(tmp_path):
    """SQLite-backend parity with RedisMediaEvidenceStore's identical
    method (docs/architecture/phase-4-crawler-fingerprinter-bridge.md) --
    a job successfully forwarded to the fingerprinter leaves `claimed`
    without ever being reported as a fingerprint verdict."""
    store = SQLiteMediaEvidenceStore(path=str(tmp_path / "media_evidence.db"))

    try:
        asset_id = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = store.claim_next_fingerprint_job(worker_id="bridge-1")
        assert job is not None

        assert store.mark_fingerprint_job_forwarded(asset_id, job.token, fingerprint_job_id="fp-job-1") is True

        jobs = store.get_fingerprint_jobs()
        assert jobs[0]["status"] == "forwarded"
        assert jobs[0]["fingerprint_job_id"] == "fp-job-1"

        asset = store.list_media_assets()[0]
        assert asset["status"] == "forwarded"
        assert asset["match_confidence"] is None
        assert asset["matched_title"] is None

        # A stale token (already reclaimed/completed by someone else) must
        # not be accepted.
        assert store.mark_fingerprint_job_forwarded(asset_id, "not-the-token", fingerprint_job_id="x") is False
    finally:
        store.close()


def test_retryable_failure_then_permanent_failure(tmp_path):
    store = SQLiteMediaEvidenceStore(
        path=str(tmp_path / "media_evidence.db"), max_retries=2, base_backoff=0.0, max_backoff=0.0
    )

    try:
        asset_id = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")

        job = store.claim_next_fingerprint_job(worker_id="w1")
        assert job is not None
        assert store.fail_fingerprint_job(
            asset_id, job.token, error_class="network", last_error="timeout", retryable=True
        ) is True

        jobs = store.get_fingerprint_jobs()
        assert jobs[0]["status"] == JOB_RETRY_SCHEDULED
        assert jobs[0]["retry_count"] == 1

        # Backoff is 0 in this test, so the retry is immediately due.
        reclaimed, requeued = store.reclaim_expired_jobs()
        assert requeued == 1
        jobs = store.get_fingerprint_jobs()
        assert jobs[0]["status"] == JOB_QUEUED

        job2 = store.claim_next_fingerprint_job(worker_id="w2")
        assert job2 is not None
        assert store.fail_fingerprint_job(
            asset_id, job2.token, error_class="network", last_error="timeout again", retryable=True
        ) is True

        jobs = store.get_fingerprint_jobs()
        assert jobs[0]["status"] == JOB_PERMANENT_FAILURE
        asset = store.list_media_assets()[0]
        assert asset["status"] == JOB_PERMANENT_FAILURE
    finally:
        store.close()


def test_non_retryable_failure_is_immediately_permanent(tmp_path):
    store = SQLiteMediaEvidenceStore(path=str(tmp_path / "media_evidence.db"), max_retries=5)

    try:
        asset_id = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video")
        job = store.claim_next_fingerprint_job(worker_id="w1")
        assert job is not None

        assert store.fail_fingerprint_job(
            asset_id, job.token, error_class="corrupt_media", last_error="cannot decode", retryable=False
        ) is True

        jobs = store.get_fingerprint_jobs()
        assert jobs[0]["status"] == JOB_PERMANENT_FAILURE
        assert jobs[0]["retry_count"] == 1
    finally:
        store.close()
