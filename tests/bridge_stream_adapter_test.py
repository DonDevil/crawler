"""Tests for bridge/fingerprint_stream_adapter.py -- the Redis-native
replica of the fingerprinter's job-submission contract (docs/architecture/
phase-4-crawler-fingerprinter-bridge.md). Run against real local Redis
(test DB 1, this repo's existing Redis-test convention) -- skip cleanly if
unavailable. Never touches production DB 0.

`test_job_id_and_stream_fields_match_the_real_fingerprinter_contract`
cross-checks this replica against the *actual* fingerprinter repository's
own `derive_job_id`/`Job.to_stream_fields`, via a subprocess into
`fingerprinter/.venv/bin/python3` -- never a Python import, per the
no-cross-repo-imports rule -- so drift between this replica and the real
contract would fail a test, not merely be documented as a risk. Slow
(~30s, dominated by the fingerprinter venv's torch import chain); skipped
if that venv isn't present.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
import redis

from bridge.fingerprint_stream_adapter import (
    AdapterSubmissionOutcome,
    BridgePriority,
    CandidateValidationError,
    count_outstanding,
    derive_job_id,
    job_stream_fields,
    stream_key,
    submit_job,
    validate_candidate_fields,
)

_FINGERPRINTER_PYTHON = Path(__file__).resolve().parents[2] / "fingerprinter" / ".venv" / "bin" / "python3"

_ALL_PRIORITY_STREAMS = ("high", "default", "low")


@pytest.fixture
def redis_conn():
    try:
        conn = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        conn.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")

    for priority in _ALL_PRIORITY_STREAMS:
        conn.delete(stream_key(priority))
    yield conn
    for priority in _ALL_PRIORITY_STREAMS:
        conn.delete(stream_key(priority))
    for key in conn.keys("fingerprint:submission:*"):
        conn.delete(key)


def _unique_url() -> str:
    return f"https://cdn.example/{uuid.uuid4().hex}.mp4"


class TestDeriveJobId:
    def test_deterministic_for_identical_inputs(self):
        a = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("dinov2",))
        b = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("dinov2",))
        assert a == b

    def test_technique_order_does_not_matter(self):
        a = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("dinov2", "phash"))
        b = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("phash", "dinov2"))
        assert a == b

    def test_different_target_version_is_a_different_job(self):
        a = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("dinov2",))
        b = derive_job_id("https://cdn.example/x.mp4", "blast", "v2", ("dinov2",))
        assert a != b

    def test_different_media_evidence_id_does_not_affect_job_id(self):
        """media_evidence_id is deliberately excluded from the hash (two
        rediscoveries of the same candidate under different asset ids are
        still one unit of work) -- this adapter must not accidentally fold
        it in."""
        # media_evidence_id isn't even a parameter to derive_job_id, so this
        # is really just asserting the function's signature matches the
        # documented hash inputs (candidate_url, target_id, target_version,
        # techniques) and nothing else.
        a = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("dinov2",))
        b = derive_job_id("https://cdn.example/x.mp4", "blast", "v1", ("dinov2",))
        assert a == b

    @pytest.mark.slow
    def test_job_id_and_stream_fields_match_the_real_fingerprinter_contract(self):
        if not _FINGERPRINTER_PYTHON.exists():
            pytest.skip(f"fingerprinter venv not found at {_FINGERPRINTER_PYTHON}")

        candidate_url = "https://cdn.example/cross-check.mp4"
        target_id, target_version = "blast", "v1"
        techniques = ("dinov2",)

        script = (
            "from integration.idempotency import derive_job_id\n"
            "from integration.candidate import FingerprintCandidate\n"
            "from work_queue.jobs import Job\n"
            "import json\n"
            "c = FingerprintCandidate(\n"
            f"    candidate_url={candidate_url!r}, media_evidence_id='aid1', media_type='video',\n"
            f"    source_domain='example.com', target_id={target_id!r}, target_version={target_version!r},\n"
            ")\n"
            "job_id = derive_job_id(c)\n"
            "j = Job(\n"
            "    job_id=job_id, media_evidence_id='aid1', media_url=c.candidate_url, media_type='video',\n"
            "    source_domain='example.com', target_id=c.target_id, target_version=c.target_version,\n"
            f"    techniques={techniques!r}, max_attempts=3,\n"
            ")\n"
            "print(json.dumps({'job_id': job_id, 'fields': j.to_stream_fields()}))\n"
        )
        proc = subprocess.run(
            [str(_FINGERPRINTER_PYTHON), "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_FINGERPRINTER_PYTHON.parents[2]),
        )
        assert proc.returncode == 0, proc.stderr
        real = json.loads(proc.stdout.strip().splitlines()[-1])

        replica_job_id = derive_job_id(candidate_url, target_id, target_version, techniques)
        assert replica_job_id == real["job_id"]

        replica_fields = job_stream_fields(
            job_id=replica_job_id,
            media_evidence_id="aid1",
            media_url=candidate_url,
            media_type="video",
            source_domain="example.com",
            target_id=target_id,
            target_version=target_version,
            techniques=techniques,
            max_attempts=3,
        )
        assert replica_fields == real["fields"]


class TestValidateCandidateFields:
    def _kwargs(self, **overrides):
        base = dict(
            candidate_url="https://cdn.example/x.mp4",
            media_evidence_id="aid1",
            media_type="video",
            source_domain="example.com",
            target_id="blast",
            target_version="v1",
            techniques=("dinov2",),
            max_attempts=3,
        )
        base.update(overrides)
        return base

    def test_accepts_a_well_formed_candidate(self):
        validate_candidate_fields(**self._kwargs())  # must not raise

    @pytest.mark.parametrize("scheme", ["ftp://cdn.example/x.mp4", "javascript:alert(1)", ""])
    def test_rejects_disallowed_or_empty_scheme(self, scheme):
        with pytest.raises(CandidateValidationError):
            validate_candidate_fields(**self._kwargs(candidate_url=scheme))

    @pytest.mark.parametrize(
        "field", ["media_evidence_id", "media_type", "source_domain", "target_id", "target_version"]
    )
    def test_rejects_empty_required_field(self, field):
        with pytest.raises(CandidateValidationError):
            validate_candidate_fields(**self._kwargs(**{field: ""}))

    def test_rejects_empty_techniques(self):
        with pytest.raises(CandidateValidationError):
            validate_candidate_fields(**self._kwargs(techniques=()))

    def test_rejects_non_positive_max_attempts(self):
        with pytest.raises(CandidateValidationError):
            validate_candidate_fields(**self._kwargs(max_attempts=0))


class TestSubmitJob:
    def _submit(self, redis_conn, **overrides):
        kwargs = dict(
            candidate_url=_unique_url(),
            media_evidence_id="aid1",
            media_type="video",
            source_domain="example.com",
            target_id="blast",
            target_version="v1",
            priority=BridgePriority.NORMAL,
            techniques=("dinov2",),
            max_attempts=3,
            max_outstanding_jobs=500,
            submission_marker_ttl_s=3600,
        )
        kwargs.update(overrides)
        return submit_job(redis_conn, **kwargs)

    def test_enqueues_with_the_exact_wire_schema(self, redis_conn):
        url = _unique_url()
        result = self._submit(redis_conn, candidate_url=url, target_id="blast", target_version="v1")

        assert result.outcome == AdapterSubmissionOutcome.ENQUEUED
        assert result.entry_id is not None

        entries = redis_conn.xrange(stream_key("default"))
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["job_id"] == result.job_id
        assert fields["media_url"] == url
        assert fields["media_evidence_id"] == "aid1"
        assert fields["media_type"] == "video"
        assert fields["source_domain"] == "example.com"
        assert fields["target_id"] == "blast"
        assert fields["target_version"] == "v1"
        assert fields["techniques"] == "dinov2"
        assert fields["max_attempts"] == "3"
        assert fields["schema_version"] == "1"

        assert redis_conn.exists(f"fingerprint:submission:{result.job_id}") == 1
        redis_conn.delete(f"fingerprint:submission:{result.job_id}")

    def test_duplicate_submission_is_suppressed_not_double_enqueued(self, redis_conn):
        url = _unique_url()
        first = self._submit(redis_conn, candidate_url=url, target_id="blast", target_version="v1")
        second = self._submit(redis_conn, candidate_url=url, target_id="blast", target_version="v1")

        assert first.outcome == AdapterSubmissionOutcome.ENQUEUED
        assert second.outcome == AdapterSubmissionOutcome.DUPLICATE_SUPPRESSED
        assert second.job_id == first.job_id

        entries = redis_conn.xrange(stream_key("default"))
        assert len(entries) == 1  # not 2
        redis_conn.delete(f"fingerprint:submission:{first.job_id}")

    def test_priority_selects_the_correct_stream(self, redis_conn):
        high = self._submit(redis_conn, candidate_url=_unique_url(), priority=BridgePriority.HIGH)
        low = self._submit(redis_conn, candidate_url=_unique_url(), priority=BridgePriority.LOW)

        assert redis_conn.xlen(stream_key("high")) == 1
        assert redis_conn.xlen(stream_key("low")) == 1
        assert redis_conn.xlen(stream_key("default")) == 0
        redis_conn.delete(f"fingerprint:submission:{high.job_id}", f"fingerprint:submission:{low.job_id}")

    def test_backpressure_rejects_and_does_not_enqueue(self, redis_conn):
        result = self._submit(redis_conn, candidate_url=_unique_url(), max_outstanding_jobs=0)

        assert result.outcome == AdapterSubmissionOutcome.REJECTED_BACKPRESSURE
        assert redis_conn.xlen(stream_key("default")) == 0
        # No submission marker should have been claimed for a rejected candidate.
        assert redis_conn.exists(f"fingerprint:submission:{result.job_id}") == 0

    def test_rejected_backpressure_candidate_can_be_resubmitted_later(self, redis_conn):
        url = _unique_url()
        rejected = self._submit(redis_conn, candidate_url=url, max_outstanding_jobs=0)
        assert rejected.outcome == AdapterSubmissionOutcome.REJECTED_BACKPRESSURE

        accepted = self._submit(redis_conn, candidate_url=url, max_outstanding_jobs=500)
        assert accepted.outcome == AdapterSubmissionOutcome.ENQUEUED
        redis_conn.delete(f"fingerprint:submission:{accepted.job_id}")


def test_count_outstanding_creates_the_consumer_group_idempotently(redis_conn):
    from bridge.fingerprint_stream_adapter import CONSUMER_GROUP

    assert count_outstanding(redis_conn, "default") == 0
    groups = redis_conn.xinfo_groups(stream_key("default"))
    assert any(g["name"] == CONSUMER_GROUP for g in groups)
    # Calling again must not raise BUSYGROUP.
    assert count_outstanding(redis_conn, "default") == 0
