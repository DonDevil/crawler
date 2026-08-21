"""Shared contract for media evidence storage/coordination backends.

Two independent implementations satisfy this contract:
`storage.sqlite_media_evidence_store.SQLiteMediaEvidenceStore` (development/
testing, single machine) and `storage.redis_media_evidence_store.
RedisMediaEvidenceStore` (production, fleet-wide). There is no
synchronization, mirroring, or fallback between them -- a process talks to
exactly one backend for its entire run. See
docs/architecture/media-evidence-redis-design.md (design) and
docs/architecture/media-evidence-step1.md (this implementation) for the
full rationale.

This module intentionally has no dependency on Redis or sqlite3 -- it only
defines the vocabulary (dataclasses, status constants, small pure helpers)
both backends share, mirroring how `core/frontier.py` defines `Frontier`/
`FrontierClaim` independently of `core/url_frontier.py`/`core/redis_frontier.py`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

# Fingerprint Job lifecycle (docs/architecture/media-evidence-redis-design.md
# §5/§25). `claimed` and `processing` are deliberately collapsed into one
# state -- the lease/heartbeat mechanism is itself the "still alive" signal,
# so there is nothing a separate "processing" state would distinguish.
JOB_QUEUED = "queued"
JOB_CLAIMED = "claimed"
JOB_COMPLETED = "completed"
JOB_RETRY_SCHEDULED = "retry_scheduled"
JOB_PERMANENT_FAILURE = "permanent_failure"

JOB_STATUSES = (JOB_QUEUED, JOB_CLAIMED, JOB_COMPLETED, JOB_RETRY_SCHEDULED, JOB_PERMANENT_FAILURE)

# FingerprintResult.aggregate_decision values (§9).
DECISION_CONFIRMED = "confirmed"
DECISION_REJECTED = "rejected"
DECISION_UNCERTAIN = "uncertain"

AGGREGATE_DECISIONS = (DECISION_CONFIRMED, DECISION_REJECTED, DECISION_UNCERTAIN)

# Asset-level display status for a still-queued asset. Distinct from
# `JOB_QUEUED` on purpose: this is what a caller reading `MediaAsset.status`
# sees before any job has ever been claimed, matching the vocabulary used in
# §2/§25 ("discovered -> queued_for_fingerprint -> claimed -> completed").
ASSET_STATUS_QUEUED = "queued_for_fingerprint"

# §16 abuse mitigations: reject before any hashing/round trip, and bound
# free-text metadata before it is ever persisted. Values are deliberately
# generous (real URLs/errors are far shorter) -- these exist to stop
# pathological/adversarial input, not to constrain legitimate use.
MAX_URL_LENGTH = 2048
MAX_METADATA_FIELD_LENGTH = 512

# §4/§25: initial default for how many recent observations are retained in
# detail per asset. Configurable per backend constructor -- never hardcoded
# beyond this one default.
DEFAULT_MAX_OBSERVATIONS_PER_ASSET = 20

# §10: initial default cap on manifest variants retained per asset.
DEFAULT_MAX_VARIANTS_PER_ASSET = 20

# §7: initial defaults for the fingerprint job lease/heartbeat. An order of
# magnitude longer than the frontier's URL-fetch lease (90s) because
# fingerprinting is minutes, not milliseconds.
DEFAULT_FINGERPRINT_LEASE_TTL = 900.0

# §8: initial default retry budget, deliberately smaller than the frontier's
# (3) given fingerprinting's higher per-attempt cost.
DEFAULT_MAX_RETRIES = 2


class MediaEvidenceUnavailable(Exception):
    """A media evidence operation could not be completed because of an
    infrastructure failure in the backend (e.g. a Redis connection or
    timeout error), not because of the store's actual state.

    Mirrors `core.frontier.FrontierUnavailable`: callers must never fold
    this into an ordinary return value (`False`, `0`, `None`, an empty
    list) -- a caller deciding "no work" must not treat this exception as
    an empty/idle result.
    """


class InvalidMediaURLError(ValueError):
    """Raised when a URL passed to `record_media_link` cannot be used as
    media evidence -- empty after cleaning, or longer than
    `MAX_URL_LENGTH` (§16: rejected before any key is computed or any
    round trip is made)."""


@dataclass(frozen=True)
class FingerprintJob:
    """A claimed unit of fingerprint work, returned by
    `claim_next_fingerprint_job`.

    `token` is the sole proof of ownership -- every subsequent
    `renew_job_lease`/`complete_fingerprint_job`/`fail_fingerprint_job` call
    must present it, and a backend must reject one that no longer matches
    the job's current claim (§6, §10).
    """

    asset_id: str
    token: str
    canonical_url: str
    media_type: str
    priority: int
    retry_count: int
    lease_expires_at: float
    # Which fingerprinter TargetRegistry (target_id, target_version) this
    # job's asset should eventually be checked against -- `None` for a
    # crawler run with no target scope configured (docs/architecture/
    # phase-3-target-registration-and-scoping.md). Fixed at job-creation
    # time, like every other field this dataclass carries; never rewritten
    # by rediscovery of the same asset under a different scope (mirrors the
    # "one job per asset, for its whole lifetime" invariant the store
    # already enforces for `priority`/`media_type`).
    target_id: Optional[str] = None
    target_version: Optional[str] = None


@dataclass(frozen=True)
class FingerprintResult:
    """Durable fingerprint evidence produced by the (not-yet-built)
    fingerprinter (§9). Never carries large binary data -- embeddings,
    frames, or media bytes belong outside Media Evidence entirely.
    """

    aggregate_decision: str
    confidence: Optional[float] = None
    dinov2_similarity: Optional[float] = None
    phash_score: Optional[float] = None
    audio_score: Optional[float] = None
    temporal_verified: Optional[bool] = None
    algorithm_versions: Optional[dict[str, str]] = None
    worker_id: str = ""
    matched_title: Optional[str] = None
    processed_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.aggregate_decision not in AGGREGATE_DECISIONS:
            raise ValueError(
                f"aggregate_decision must be one of {AGGREGATE_DECISIONS}, got {self.aggregate_decision!r}"
            )


def compute_discovery_id(cleaned_url: str) -> str:
    """Deterministic asset identity: sha256 of the normalized media URL
    (§3). Any machine can compute this locally, with no coordination and no
    round trip, before ever talking to a backend.
    """
    return hashlib.sha256(cleaned_url.encode("utf-8")).hexdigest()


def derive_asset_status(job_status: str, aggregate_decision: Optional[str]) -> str:
    """Compute the asset's displayed status from Job state + Result
    decision, rather than storing it as an independently-writable field.

    This closes a confirmed SQLite bug (§1a finding (b)): a status field
    written by both the rediscovery path and the job-transition path could
    desync from the job's actual state. Deriving it structurally rules that
    out -- rediscovery never writes to this field at all.
    """
    if job_status == JOB_COMPLETED:
        return aggregate_decision or DECISION_UNCERTAIN
    if job_status == JOB_QUEUED:
        return ASSET_STATUS_QUEUED
    return job_status


def validate_media_url_length(cleaned_url: str) -> None:
    """Reject an oversized URL before any hash/key is computed (§16)."""
    if len(cleaned_url) > MAX_URL_LENGTH:
        raise InvalidMediaURLError(
            f"Media URL exceeds MAX_URL_LENGTH ({MAX_URL_LENGTH} chars): {len(cleaned_url)} chars"
        )


def truncate_metadata(value: Optional[str], max_length: int = MAX_METADATA_FIELD_LENGTH) -> Optional[str]:
    """Bound a free-text metadata field (mime_type, matched_title,
    last_error, error_class) before it is persisted (§16) -- an unbounded
    error string from a malformed downstream failure is a realistic
    growth vector.
    """
    if value is None:
        return None
    return value[:max_length]


class MediaEvidenceStore(Protocol):
    """Behavioral contract implemented by every media evidence backend.

    Error contract mirrors `core.frontier.Frontier`: a backend that cannot
    complete an operation because of an infrastructure failure must raise
    `MediaEvidenceUnavailable` rather than returning a value that could be
    mistaken for legitimate state.
    """

    def record_media_link(
        self,
        *,
        url: str,
        source_page: Optional[str] = None,
        referrer_url: Optional[str] = None,
        discovered_by: str = "unknown",
        discovery_method: str = "parser",
        media_type: Optional[str] = None,
        mime_type: Optional[str] = None,
        content_length: Optional[int] = None,
        priority: int = 10,
    ) -> str:
        """Record one discovery event for a media URL.

        Idempotent asset creation, unconditional observation append, and
        job creation gated to first-ever discovery of the asset -- see
        docs/architecture/media-evidence-redis-design.md §1a/§11. Returns
        the asset's `discovery_id`/identity string, stable across every
        call for the same normalized URL.
        """
        ...

    def record_manifest_variants(self, asset_id: str, variants: list[dict]) -> None:
        """Attach bounded, descriptive HLS/DASH variant metadata to a
        manifest asset. Variants are never independently fingerprinted or
        given their own job (§10)."""
        ...

    def list_media_assets(self, *, limit: Optional[int] = None) -> list[dict]:
        """Return known assets, most recently seen first."""
        ...

    def list_observations(self, asset_id: str) -> list[dict]:
        """Return this asset's retained observations.

        Contract differs by backend (§17): SQLite returns full history;
        Redis returns only the most recent `max_observations_per_asset`
        (the exact count is always available via the asset's
        `observation_count` field from `list_media_assets`).
        """
        ...

    def list_manifest_variants(self, asset_id: str) -> list[dict]:
        """Return this asset's manifest variants, ordered by bandwidth
        ascending."""
        ...

    def get_fingerprint_jobs(self, statuses: Optional[Sequence[str]] = None) -> list[dict]:
        """Return fingerprint jobs, optionally filtered to `statuses`.

        Introspection/CLI/testing surface, not a hot-path operation --
        renamed from the SQLite-era `get_sample_jobs` for consistency with
        the rest of this interface's `fingerprint_job` vocabulary (§17).
        """
        ...

    def claim_next_fingerprint_job(self, worker_id: str) -> Optional[FingerprintJob]:
        """Atomically claim the highest-priority eligible job, or return
        `None` if the queue is empty. Distributed-safe: concurrent callers
        across processes/machines never receive the same job (§6, §8)."""
        ...

    def renew_job_lease(self, asset_id: str, token: str) -> bool:
        """Extend a claimed job's lease iff `token` is still the current
        owner (§7, §10). Returns `False` for a stale/unknown claim."""
        ...

    def complete_fingerprint_job(self, asset_id: str, token: str, *, result: FingerprintResult) -> bool:
        """Record a durable `FingerprintResult` and terminate the job, iff
        `token` is still the current owner (§9, §10). Returns `False` for a
        stale/unknown claim -- the caller must not treat this as success."""
        ...

    def fail_fingerprint_job(
        self,
        asset_id: str,
        token: str,
        *,
        error_class: str,
        last_error: str,
        retryable: bool,
    ) -> bool:
        """Record a failed attempt, iff `token` is still the current owner
        (§8, §10). `retryable` is the caller's classification, applied
        mechanically: retryable failures get exponential backoff up to
        `max_retries`, after which -- like any exhausted or non-retryable
        failure -- the job becomes `permanent_failure`. Returns `False` for
        a stale/unknown claim."""
        ...

    def reclaim_expired_jobs(self, batch_size: int = 200) -> tuple[int, int]:
        """Reclaim jobs whose lease expired without a renewal or
        completion, and promote due `retry_scheduled` jobs back onto the
        queue (§6, §8). Returns `(reclaimed, requeued)`. Safe to call
        redundantly/concurrently from every worker -- idempotent by
        construction."""
        ...

    def get_status_counts(self) -> dict[str, int]:
        """Return job counts per lifecycle state (§25): `queued`,
        `claimed`, `retry_scheduled`, `permanent_failure`, `completed`."""
        ...

    def clear(self) -> None:
        """Remove all stored state (testing/reset only)."""
        ...

    def close(self) -> None:
        """Release the backend's underlying connection(s)."""
        ...
