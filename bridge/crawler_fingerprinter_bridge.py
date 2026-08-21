"""The Phase 4 bridge itself: claims jobs from the crawler's own
`evidence:jobs:queue` (via the real `RedisMediaEvidenceStore` API -- an
in-repo import, not a cross-repo one) and forwards them onto the
fingerprinter's `fingerprint:jobs:stream:{priority}` contract (via
`fingerprint_stream_adapter.py`'s Redis-native replica of that contract).

See docs/architecture/phase-4-crawler-fingerprinter-bridge.md for the full
design, crash-safety analysis, and delivery-semantics writeup this module
implements. In short:

    claim (existing crawler API, CAS token)
        -> validate target scope is present and still registered
        -> validate candidate fields (fingerprinter's own admission rules)
        -> submit to the fingerprinter stream (backpressure -> dedup -> XADD)
        -> only now, mark the source job `forwarded` (never `completed` --
           see JOB_FORWARDED's docstring)

A crash at any point before the final step leaves the source job `claimed`
with an unrenewed lease; the crawler's own `reclaim_expired_jobs()` (already
used by `CrawlerManager`'s recovery loop, reused verbatim here) requeues it
for another claim attempt. Because `derive_job_id` is deterministic and the
fingerprinter's own `SET NX` submission marker survives across bridge
crashes/restarts, a resubmitted candidate after a crash either enqueues
successfully (if the first attempt never reached `XADD`) or comes back
`DUPLICATE_SUPPRESSED` (if it did) -- never a second stream entry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Tuple

import redis
from loguru import logger

from bridge.fingerprint_stream_adapter import (
    DEFAULT_TECHNIQUES,
    AdapterSubmissionOutcome,
    BridgePriority,
    CandidateValidationError,
    submit_job,
    validate_candidate_fields,
)
from core.target_scope import TargetScope, TargetScopeError, verify_target_registered
from storage.media_evidence_store import FingerprintJob, MediaEvidenceUnavailable
from storage.redis_media_evidence_store import RedisMediaEvidenceStore


@dataclass
class BridgeMetrics:
    """Rule 16's minimum counter set, plus `active_jobs` -- process-local,
    read directly by tests and by `bridge/main.py`'s shutdown log line. Not
    a new telemetry system: matches the plain-attribute-counter style
    already used elsewhere in this repo (e.g. the benchmark harness's
    `ResourceMonitor`) rather than inventing a metrics framework."""

    jobs_claimed: int = 0
    jobs_submitted: int = 0
    jobs_acknowledged: int = 0
    jobs_retried: int = 0
    jobs_rejected: int = 0
    jobs_failed: int = 0
    jobs_duplicated: int = 0
    active_jobs: int = 0


@dataclass(frozen=True)
class BridgeRuntimeConfig:
    """Runtime tuning for one `CrawlerFingerprinterBridge` instance --
    populated from `core.config.BridgeConfig` by `bridge/main.py`, kept as
    its own plain dataclass here so this module has no pydantic dependency
    and can be constructed directly by tests without loading `config.yaml`."""

    max_outstanding_jobs: int = 500
    submission_marker_ttl_s: int = 24 * 60 * 60
    priority_high_max: int = 5
    priority_low_min: int = 15
    max_attempts: int = 3
    techniques: Tuple[str, ...] = DEFAULT_TECHNIQUES
    idle_sleep_seconds: float = 2.0
    reclaim_interval_seconds: float = 60.0
    reclaim_batch_size: int = 200


class CrawlerFingerprinterBridge:
    """One bridge worker. Single-threaded, bounded-work-per-iteration by
    construction (`process_one()` claims and fully resolves exactly one
    job) -- the brief's "prefer one bridge worker/process with bounded
    concurrency" default; nothing about the source or destination contract
    requires more than one process (both are already safe for many
    independent claimers, so running several `CrawlerFingerprinterBridge`
    instances concurrently, e.g. one per host, is safe with zero code
    changes if throughput ever demands it -- not built here since nothing
    demonstrates the need yet)."""

    def __init__(
        self,
        store: RedisMediaEvidenceStore,
        worker_id: str,
        config: BridgeRuntimeConfig = BridgeRuntimeConfig(),
    ):
        self._store = store
        self._worker_id = worker_id
        self._config = config
        self.metrics = BridgeMetrics()
        self._stop = False
        self._last_reclaim = 0.0

    def stop(self) -> None:
        """Cooperative shutdown flag, checked once per `run_forever()`
        iteration -- never interrupts a job already in flight, so a job
        that is mid-`_process_claimed_job` when `stop()` is called still
        finishes (or fails cleanly) before the loop exits."""
        self._stop = True

    def _priority_band(self, crawler_priority: int) -> BridgePriority:
        """docs/architecture/phase-4-crawler-fingerprinter-bridge.md,
        "Priority propagation" -- explicit, configurable, documented
        mapping (never a silent flatten-to-one-priority)."""
        if crawler_priority <= self._config.priority_high_max:
            return BridgePriority.HIGH
        if crawler_priority >= self._config.priority_low_min:
            return BridgePriority.LOW
        return BridgePriority.NORMAL

    def _read_source_domain(self, asset_id: str) -> str:
        """One extra `HGET` against the crawler's own `evidence:asset:{aid}`
        hash -- exactly the mapping the prior Phase 3 bridge audit specified
        (docs/architecture/phase-3-crawler-fingerprinter-bridge.md §5:
        "one extra HGETALL, already read by the store's own
        list_media_assets"). Reads a documented field of this repo's own
        key convention via the store's public `namespace`/`redis_conn`
        attributes -- not a private-method reach-in, and not a second
        connection."""
        value = self._store.redis_conn.hget(f"{self._store.namespace}:asset:{asset_id}", "source_domain")
        return value or ""

    def _permanently_reject(self, job: FingerprintJob, *, error_class: str, detail: str) -> None:
        logger.warning(f"bridge: permanent reject asset_id={job.asset_id} error_class={error_class}: {detail}")
        try:
            acted = self._store.fail_fingerprint_job(
                job.asset_id, job.token, error_class=error_class, last_error=detail, retryable=False
            )
        except MediaEvidenceUnavailable as exc:
            logger.error(f"bridge: could not record permanent rejection for asset_id={job.asset_id}: {exc}")
            raise
        if acted:
            self.metrics.jobs_rejected += 1

    def _retry_later(self, job: FingerprintJob, *, error_class: str, detail: str) -> None:
        logger.warning(f"bridge: retryable failure asset_id={job.asset_id} error_class={error_class}: {detail}")
        try:
            acted = self._store.fail_fingerprint_job(
                job.asset_id, job.token, error_class=error_class, last_error=detail, retryable=True
            )
        except MediaEvidenceUnavailable as exc:
            logger.error(f"bridge: could not record retryable failure for asset_id={job.asset_id}: {exc}")
            raise
        if acted:
            self.metrics.jobs_retried += 1

    def _process_claimed_job(self, job: FingerprintJob) -> None:
        # Rule 3/4: target identity travels on the job or the job is
        # rejected -- never inferred, never fabricated, never auto-registered.
        if not job.target_id or not job.target_version:
            self._permanently_reject(
                job,
                error_class="missing_target_scope",
                detail=(
                    "evidence job carries no target_id/target_version (created by an unscoped crawler "
                    "run, docs/architecture/phase-3-target-registration-and-scoping.md); the bridge "
                    "never fabricates target identity, so this candidate cannot be forwarded"
                ),
            )
            return

        try:
            scope = TargetScope(target_id=job.target_id, target_version=job.target_version)
        except TargetScopeError as exc:
            self._permanently_reject(job, error_class="invalid_target_scope", detail=str(exc))
            return

        # Defense-in-depth re-check (Case J): the job's target was verified
        # once, at crawler-run startup (Phase 3); re-checking here is cheap
        # (one EXISTS, reusing this repo's own core/target_scope.py -- an
        # in-repo import) and avoids burning a fingerprint worker's DINOv2
        # cost on a target that no longer resolves.
        try:
            registered = verify_target_registered(self._store.redis_conn, scope)
        except redis.RedisError as exc:
            logger.error(f"bridge: Redis error verifying target registration for asset_id={job.asset_id}: {exc}")
            raise
        if not registered:
            self._permanently_reject(
                job,
                error_class="target_not_registered",
                detail=f"target {scope.target_id!r}/{scope.target_version!r} not found in TargetRegistry",
            )
            return

        source_domain = self._read_source_domain(job.asset_id)

        try:
            validate_candidate_fields(
                candidate_url=job.canonical_url,
                media_evidence_id=job.asset_id,
                media_type=job.media_type,
                source_domain=source_domain,
                target_id=scope.target_id,
                target_version=scope.target_version,
                techniques=self._config.techniques,
                max_attempts=self._config.max_attempts,
            )
        except CandidateValidationError as exc:
            self._permanently_reject(job, error_class="invalid_candidate", detail=str(exc))
            return

        priority = self._priority_band(job.priority)

        try:
            result = submit_job(
                self._store.redis_conn,
                candidate_url=job.canonical_url,
                media_evidence_id=job.asset_id,
                media_type=job.media_type,
                source_domain=source_domain,
                target_id=scope.target_id,
                target_version=scope.target_version,
                priority=priority,
                techniques=self._config.techniques,
                max_attempts=self._config.max_attempts,
                max_outstanding_jobs=self._config.max_outstanding_jobs,
                submission_marker_ttl_s=self._config.submission_marker_ttl_s,
            )
        except redis.RedisError as exc:
            logger.error(f"bridge: Redis error submitting asset_id={job.asset_id} to the fingerprinter stream: {exc}")
            raise

        if result.outcome == AdapterSubmissionOutcome.REJECTED_BACKPRESSURE:
            self._retry_later(job, error_class="backpressure", detail=result.detail or "")
            return

        if result.outcome == AdapterSubmissionOutcome.DUPLICATE_SUPPRESSED:
            self.metrics.jobs_duplicated += 1
            logger.info(
                f"bridge: duplicate suppressed asset_id={job.asset_id} job_id={result.job_id} "
                "(already forwarded by an earlier attempt)"
            )
        else:
            self.metrics.jobs_submitted += 1
            logger.info(
                f"bridge: submitted asset_id={job.asset_id} job_id={result.job_id} "
                f"entry_id={result.entry_id} priority={priority.value}"
            )

        # Rule 7: the source job is acknowledged only now, after the
        # fingerprinter stream write is durable (ENQUEUED) or already was
        # (DUPLICATE_SUPPRESSED) -- never earlier.
        try:
            acked = self._store.mark_fingerprint_job_forwarded(
                job.asset_id, job.token, fingerprint_job_id=result.job_id
            )
        except MediaEvidenceUnavailable as exc:
            logger.error(
                f"bridge: forwarded asset_id={job.asset_id} (job_id={result.job_id}) but could not "
                f"acknowledge the source job (source Redis unavailable): {exc}. The claim's lease will "
                "expire and be reclaimed; re-forwarding is safe (destination-side dedup via job_id)."
            )
            raise
        if acked:
            self.metrics.jobs_acknowledged += 1
        else:
            logger.warning(
                f"bridge: source ack for asset_id={job.asset_id} was stale (the claim was already "
                "reclaimed, e.g. by another bridge instance after a lease expiry) -- the job may be "
                "reprocessed elsewhere; destination-side dedup prevents a duplicate stream entry."
            )

    def process_one(self) -> bool:
        """Claim and fully resolve exactly one evidence job. Returns
        `False` if the queue was empty (caller should idle-sleep)."""
        try:
            job = self._store.claim_next_fingerprint_job(worker_id=self._worker_id)
        except MediaEvidenceUnavailable as exc:
            logger.error(f"bridge: source Redis unavailable while claiming a job: {exc}")
            raise

        if job is None:
            return False

        self.metrics.jobs_claimed += 1
        self.metrics.active_jobs += 1
        logger.info(
            f"bridge: claimed asset_id={job.asset_id} priority={job.priority} "
            f"target_id={job.target_id!r} target_version={job.target_version!r}"
        )
        try:
            self._process_claimed_job(job)
        except Exception:
            self.metrics.jobs_failed += 1
            raise
        finally:
            self.metrics.active_jobs -= 1
        return True

    def _maybe_reclaim(self) -> None:
        """Mirrors `CrawlerManager._recovery_loop`'s use of the frontier's
        own reclaim sweep (core/crawler_manager.py) -- reuses the evidence
        store's existing `reclaim_expired_jobs()` (rule 8: no second retry
        implementation)."""
        now = time.monotonic()
        if now - self._last_reclaim < self._config.reclaim_interval_seconds:
            return
        self._last_reclaim = now
        try:
            reclaimed, requeued = self._store.reclaim_expired_jobs(self._config.reclaim_batch_size)
            if reclaimed or requeued:
                logger.debug(f"bridge: recovery sweep reclaimed={reclaimed} requeued={requeued}")
        except MediaEvidenceUnavailable as exc:
            logger.error(f"bridge: recovery sweep failed: {exc}")

    def run_forever(self) -> None:
        """Blocking loop; returns once `stop()` has been called and the
        current iteration (if any) finishes. Infrastructure failures
        (`MediaEvidenceUnavailable`, raw `redis.RedisError`) are caught here
        -- one level above every job-specific failure path -- and treated as
        rule 14's separate "Redis infrastructure failure" class: logged,
        backed off, retried on the next iteration, never routed through
        `core.network_health.HealthController` (that system is about this
        process's own Internet reachability, an unrelated failure domain)."""
        while not self._stop:
            self._maybe_reclaim()
            try:
                got_job = self.process_one()
            except (MediaEvidenceUnavailable, redis.RedisError) as exc:
                logger.error(f"bridge: infrastructure failure, backing off: {exc}")
                time.sleep(self._config.idle_sleep_seconds)
                continue
            if not got_job:
                time.sleep(self._config.idle_sleep_seconds)
