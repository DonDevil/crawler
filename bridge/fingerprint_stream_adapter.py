"""Minimal Redis-native adapter for the fingerprinter's existing job-submission
contract -- `integration.submission.FingerprintJobSubmitter.submit()` and its
collaborators (`integration/candidate.py`, `integration/idempotency.py`,
`integration/keys.py`, `integration/backpressure.py`, `work_queue/jobs.py`,
`work_queue/producer.py`, `work_queue/keys.py`), all in the sibling
`fingerprinter` repository.

Why this module exists instead of importing that class directly
(docs/architecture/phase-4-crawler-fingerprinter-bridge.md, "Architecture
decision"): `FingerprintJobSubmitter` is an in-process Python API defined in
a separate, independently-deployed repository with its own virtual
environment (`fingerprinter/.venv/`, which pulls in torch/transformers and is
not installed anywhere in this repo's `env/`). Importing it from here would
mean either merging the two environments or adding a direct cross-repo
Python import -- both explicitly forbidden (`docs/design/design-proposal-1.md`'s
own opening paragraph, restated in every phase-3/phase-12 doc on either
side). Per this phase's brief: "If the current FingerprintJobSubmitter only
exists as an in-process Python API and cannot be used from the crawler
repository without violating the independent-repository rule, design a
minimal Redis-native bridge adapter inside the appropriate repository rather
than introducing cross-repo imports."

Every constant and algorithm below is therefore a byte-for-byte replica of
the fingerprinter's own contract, verified against that repo's actual
source (not guessed, not inferred) as of the git revision this phase
inspected it at. If the fingerprinter repo ever changes any of these, this
module must be updated to match by hand -- there is no way to detect drift
across the repository boundary automatically. See the phase-4 doc's
"Limitations" for this tradeoff.

Copied from, one-to-one:

    fingerprinter/work_queue/keys.py::CONSUMER_GROUP, stream_key
    fingerprinter/work_queue/jobs.py::Job.to_stream_fields, JOB_SCHEMA_VERSION
    fingerprinter/integration/keys.py::submission_marker_key
    fingerprinter/integration/idempotency.py::derive_job_id
    fingerprinter/integration/candidate.py::FingerprintPriority,
        PRIORITY_STREAM_NAMES, FingerprintCandidate.validate,
        _ALLOWED_URL_SCHEMES
    fingerprinter/integration/backpressure.py::count_outstanding, _ensure_group
    fingerprinter/integration/submission.py::FingerprintJobSubmitter.submit
        (ordering: validate -> backpressure -> claim marker -> XADD ->
        release marker on XADD failure)
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from redis import Redis
from redis.exceptions import ResponseError

# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/work_queue/keys.py
# ---------------------------------------------------------------------
CONSUMER_GROUP = "fingerprinter-workers"
_DEFAULT_PRIORITY_STREAM = "default"


def stream_key(priority_name: str) -> str:
    return f"fingerprint:jobs:stream:{priority_name}"


# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/integration/keys.py
# ---------------------------------------------------------------------
def submission_marker_key(job_id: str) -> str:
    return f"fingerprint:submission:{job_id}"


# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/integration/candidate.py
# ---------------------------------------------------------------------
_ALLOWED_URL_SCHEMES = ("http://", "https://")

# fingerprinter/matching/aggregation.py::DINOV2_TEMPORAL_TECHNIQUE. The
# bridge has no per-candidate technique-selection signal (nothing in the
# crawler's own evidence job carries one), so every forwarded job uses the
# fingerprinter's own single-technique default, exactly as
# `FingerprintCandidate.techniques`'s default does.
DEFAULT_TECHNIQUES = ("dinov2",)


class BridgePriority(str, Enum):
    """Mirrors fingerprinter/integration/candidate.py::FingerprintPriority."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


PRIORITY_STREAM_NAMES = {
    BridgePriority.HIGH: "high",
    BridgePriority.NORMAL: _DEFAULT_PRIORITY_STREAM,
    BridgePriority.LOW: "low",
}


class CandidateValidationError(ValueError):
    """Mirrors fingerprinter/integration/candidate.py::CandidateValidationError.
    Kept as a distinct local exception type (not imported) -- see module
    docstring."""


def validate_candidate_fields(
    *,
    candidate_url: str,
    media_evidence_id: str,
    media_type: str,
    source_domain: str,
    target_id: str,
    target_version: str,
    techniques: Sequence[str],
    max_attempts: int,
) -> None:
    """Byte-for-byte replica of `FingerprintCandidate.validate()`'s checks.
    Raises `CandidateValidationError`."""
    if not candidate_url or not candidate_url.lower().startswith(_ALLOWED_URL_SCHEMES):
        raise CandidateValidationError(
            f"candidate_url must start with one of {_ALLOWED_URL_SCHEMES!r}: {candidate_url!r}"
        )
    for name, value in (
        ("media_evidence_id", media_evidence_id),
        ("media_type", media_type),
        ("source_domain", source_domain),
        ("target_id", target_id),
        ("target_version", target_version),
    ):
        if not value:
            raise CandidateValidationError(f"{name} must not be empty")
    if not techniques:
        raise CandidateValidationError("techniques must contain at least one entry")
    if max_attempts < 1:
        raise CandidateValidationError("max_attempts must be >= 1")


# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/integration/idempotency.py
# ---------------------------------------------------------------------
JOB_ID_LENGTH = 32


def derive_job_id(candidate_url: str, target_id: str, target_version: str, techniques: Sequence[str]) -> str:
    """Byte-for-byte replica of `integration.idempotency.derive_job_id`.
    Must produce the exact same digest the fingerprinter's own submitter
    would for the same inputs -- this is what makes bridge-crash-and-retry
    resubmission collapse onto the same `job_id` the fingerprinter's own
    `SET NX` marker already dedups on, rather than a bridge-local hash the
    fingerprinter has never heard of."""
    payload = json.dumps(
        {
            "candidate_url": candidate_url,
            "target_id": target_id,
            "target_version": target_version,
            "techniques": sorted(techniques),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:JOB_ID_LENGTH]


# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/work_queue/jobs.py::Job
# ---------------------------------------------------------------------
JOB_SCHEMA_VERSION = 1


def job_stream_fields(
    *,
    job_id: str,
    media_evidence_id: str,
    media_url: str,
    media_type: str,
    source_domain: str,
    target_id: str,
    target_version: str,
    techniques: Sequence[str],
    max_attempts: int,
) -> dict[str, str]:
    """Byte-for-byte replica of `Job.to_stream_fields()` -- the exact
    string->string mapping `XADD` must receive for
    `work_queue.jobs.Job.from_stream_fields()` (unchanged, fingerprinter
    repo) to parse it back into a valid `Job` on the worker side."""
    return {
        "job_id": job_id,
        "media_evidence_id": media_evidence_id,
        "media_url": media_url,
        "media_type": media_type,
        "source_domain": source_domain,
        "target_id": target_id,
        "target_version": target_version,
        "techniques": ",".join(techniques),
        "max_attempts": str(max_attempts),
        "schema_version": str(JOB_SCHEMA_VERSION),
    }


# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/integration/backpressure.py
# ---------------------------------------------------------------------
def _ensure_group(redis_client: Redis, stream: str) -> None:
    try:
        redis_client.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def count_outstanding(redis_client: Redis, priority_name: str) -> Optional[int]:
    """Byte-for-byte replica of `integration.backpressure.count_outstanding`."""
    stream = stream_key(priority_name)
    _ensure_group(redis_client, stream)
    groups = redis_client.xinfo_groups(stream)

    for group in groups:
        if group.get("name") != CONSUMER_GROUP:
            continue
        lag = group.get("lag")
        pending = group.get("pending") or 0
        if lag is None:
            return None
        return int(lag) + int(pending)
    return 0


# ---------------------------------------------------------------------
# Copied verbatim (ordering + marker-release-on-failure semantics) from
# fingerprinter/integration/submission.py::FingerprintJobSubmitter.submit
# ---------------------------------------------------------------------
class AdapterSubmissionOutcome(str, Enum):
    ENQUEUED = "enqueued"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    REJECTED_BACKPRESSURE = "rejected_backpressure"


@dataclass(frozen=True)
class AdapterSubmissionResult:
    outcome: AdapterSubmissionOutcome
    job_id: str
    entry_id: Optional[str] = None
    detail: Optional[str] = None


def _claim_submission_marker(redis_client: Redis, job_id: str, ttl_s: int) -> bool:
    claimed = redis_client.set(submission_marker_key(job_id), str(time.time()), nx=True, ex=ttl_s)
    return bool(claimed)


def submit_job(
    redis_client: Redis,
    *,
    candidate_url: str,
    media_evidence_id: str,
    media_type: str,
    source_domain: str,
    target_id: str,
    target_version: str,
    priority: BridgePriority,
    techniques: Sequence[str] = DEFAULT_TECHNIQUES,
    max_attempts: int,
    max_outstanding_jobs: int,
    submission_marker_ttl_s: int,
) -> AdapterSubmissionResult:
    """Replica of `FingerprintJobSubmitter.submit()`, minus the
    `candidate.validate()` step (the caller must call
    `validate_candidate_fields()` first -- kept as a separate call so a
    validation failure and a Redis-level submission attempt are never
    conflated by this function's own control flow).

    Ordering, deliberately preserved from the original: check backpressure
    (read-only) -> claim the dedup marker (the one write before enqueue) ->
    enqueue -> release the marker if `XADD` itself fails. Uses
    `redis_client` for both the backpressure read and the marker/XADD
    writes -- the caller is expected to pass the *same* Redis connection
    the crawler's own evidence store already opened (this deployment's
    evidence Redis and the fingerprinter's registry/stream Redis are
    confirmed the same physical instance -- see docs/architecture/
    phase-3-crawler-fingerprinter-bridge.md §7), never a second connection.
    """
    job_id = derive_job_id(candidate_url, target_id, target_version, techniques)
    priority_name = PRIORITY_STREAM_NAMES[priority]

    outstanding = count_outstanding(redis_client, priority_name)
    if outstanding is None or outstanding >= max_outstanding_jobs:
        return AdapterSubmissionResult(
            AdapterSubmissionOutcome.REJECTED_BACKPRESSURE,
            job_id=job_id,
            detail=f"outstanding={outstanding!r} >= max_outstanding_jobs={max_outstanding_jobs}",
        )

    if not _claim_submission_marker(redis_client, job_id, submission_marker_ttl_s):
        return AdapterSubmissionResult(AdapterSubmissionOutcome.DUPLICATE_SUPPRESSED, job_id=job_id)

    fields = job_stream_fields(
        job_id=job_id,
        media_evidence_id=media_evidence_id,
        media_url=candidate_url,
        media_type=media_type,
        source_domain=source_domain,
        target_id=target_id,
        target_version=target_version,
        techniques=techniques,
        max_attempts=max_attempts,
    )
    try:
        entry_id = redis_client.xadd(stream_key(priority_name), fields)
    except Exception:
        # The marker promised "this will be enqueued"; since it wasn't,
        # release it so a caller's retry isn't falsely suppressed as a
        # duplicate of a submission that never reached the stream (mirrors
        # FingerprintJobSubmitter.submit()'s identical comment).
        redis_client.delete(submission_marker_key(job_id))
        raise

    return AdapterSubmissionResult(AdapterSubmissionOutcome.ENQUEUED, job_id=job_id, entry_id=entry_id)
