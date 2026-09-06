"""The reverse-direction half of the crawler<->fingerprinter bridge (see
`bridge/crawler_fingerprinter_bridge.py`'s forward direction): consumes the
fingerprinter's `fingerprint:results:stream:{priority}` (via
`fingerprint_result_adapter.py`'s Redis-native replica of that contract) and
completes the corresponding crawler-side forwarded job with the terminal
verdict, using `RedisMediaEvidenceStore.complete_forwarded_fingerprint_job`.

Deliberately a separate class/process from `CrawlerFingerprinterBridge`,
which only ever claims->validates->forwards in the crawler->fingerprinter
direction; mixing both directions into one class would blur that class's
single responsibility. This module reuses the exact same Redis Streams
consumer-group idiom `worker.fingerprint_worker.Worker` (fingerprinter repo)
already uses for its own job stream -- XREADGROUP, XAUTOCLAIM for stale
entries, XACK on completion -- under a distinct consumer group name
(`fingerprint_result_adapter.CONSUMER_GROUP`) so it never collides with the
fingerprinter's own `"fingerprinter-workers"` group, which reads a different
stream entirely.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import redis
from loguru import logger

from bridge.fingerprint_result_adapter import (
    CONSUMER_GROUP,
    MalformedResultEventError,
    MalformedResultRecordError,
    ResolvedResult,
    ResultDecision,
    parse_result_event,
    parse_result_record,
    result_key,
    results_stream_key,
)
from storage.media_evidence_store import FingerprintResult, MediaEvidenceUnavailable
from storage.redis_media_evidence_store import RedisMediaEvidenceStore

_DECISION_TO_AGGREGATE = {
    ResultDecision.MATCH: "confirmed",
    ResultDecision.NO_MATCH: "rejected",
    ResultDecision.PROCESSING_FAILURE: "uncertain",
}


@dataclass
class ResultConsumerMetrics:
    """Process-local counters, read directly by tests and by
    `result_consumer_main.py`'s shutdown log line -- same plain-attribute
    style as `BridgeMetrics`."""

    results_claimed: int = 0
    matches_recorded: int = 0
    no_matches_recorded: int = 0
    failures_recorded: int = 0
    stale_skipped: int = 0
    malformed_rejected: int = 0
    evidence_missing: int = 0


def _extract_dinov2_similarity(evidence_json: Optional[str]) -> Optional[float]:
    """Best-effort: the DINOv2 technique's own `score` from the
    fingerprinter's per-technique evidence JSON (matching.aggregation.
    TechniqueEvidence, technique="dinov2"). `None` if absent/unparseable --
    never fatal, this is a convenience field on top of the verbatim
    `evidence` JSON passthrough, not the field of record."""
    if not evidence_json:
        return None
    try:
        entries = json.loads(evidence_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("technique") == "dinov2":
            score = entry.get("score")
            return float(score) if isinstance(score, (int, float)) else None
    return None


def _build_algorithm_versions(evidence_json: Optional[str]) -> Optional[dict[str, str]]:
    if not evidence_json:
        return None
    try:
        entries = json.loads(evidence_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    versions = {
        entry["technique"]: entry["matcher_version"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("technique") and entry.get("matcher_version")
    }
    return versions or None


def _to_fingerprint_result(resolved: ResolvedResult) -> FingerprintResult:
    aggregate_decision = _DECISION_TO_AGGREGATE.get(resolved.decision)
    if aggregate_decision is None:
        raise MalformedResultRecordError(
            f"job_id={resolved.job_id!r}: unrecognized decision {resolved.decision!r}"
        )
    return FingerprintResult(
        aggregate_decision=aggregate_decision,
        confidence=resolved.confidence,
        dinov2_similarity=_extract_dinov2_similarity(resolved.evidence),
        algorithm_versions=_build_algorithm_versions(resolved.evidence),
        worker_id=resolved.worker_id,
        processed_at=(
            str(resolved.processing_completed_at) if resolved.processing_completed_at is not None else None
        ),
        evidence=resolved.evidence,
    )


def _blocking_read_client(store: RedisMediaEvidenceStore, block_ms: int) -> redis.Redis:
    """A dedicated Redis client for this consumer's long blocking reads
    (XREADGROUP/XAUTOCLAIM) -- deliberately NOT `store.redis_conn`.

    `RedisMediaEvidenceStore` constructs its connection with no explicit
    `socket_timeout` (storage/redis_media_evidence_store.py), which this
    environment's installed redis-py defaults to 5 seconds (`redis.
    _defaults.DEFAULT_SOCKET_TIMEOUT`). That default is fine for the
    store's own Lua-script round trips, which never block, but a `BLOCK
    {block_ms}` read's server-side wait can itself legitimately take up to
    `block_ms` -- with this consumer's own `block_ms=5000` default, that
    races the client's 5s socket read timeout with essentially zero margin
    on every single empty poll, and loses: redis-py's retry policy then
    silently retries the same blocked read with growing backoff (observed
    empirically: three consecutive empty polls took 25s, then 41s, then
    59s instead of ~5s each) before either succeeding very late or
    eventually raising `redis.exceptions.TimeoutError: Timeout reading
    from ...` -- exactly the "runs for a bit, then infrastructure
    error / timeout reading from socket" symptom this connection exists to
    eliminate.

    Same host/port/db/decoding as `store.redis_conn` (confirmed the same
    physical Redis instance, docs/architecture/
    phase-3-crawler-fingerprinter-bridge.md §7), but with a `socket_timeout`
    generously larger than any `block_ms` this consumer will realistically
    be configured with, so the client never gives up on a read the server
    was always going to answer in time. This installed redis-py version
    already retries `TimeoutError` by default (the older `retry_on_timeout`
    constructor flag is deprecated precisely because of that -- passing it
    only produces a `DeprecationWarning` here, no behavior change), so it
    is deliberately not passed.
    """
    kwargs = store.redis_conn.connection_pool.connection_kwargs
    return redis.Redis(
        host=kwargs.get("host", "localhost"),
        port=kwargs.get("port", 6379),
        db=kwargs.get("db", 0),
        decode_responses=kwargs.get("decode_responses", True),
        socket_keepalive=True,
        health_check_interval=30,
        socket_timeout=max(block_ms / 1000.0 + 10.0, 15.0),
    )


class FingerprintResultConsumer:
    """One result-consumer worker. Single-threaded, bounded-work-per-
    iteration (`process_one()` claims and fully resolves the batch one
    XREADGROUP call returns) -- mirrors `CrawlerFingerprinterBridge`'s own
    convention; running several instances concurrently is safe (Streams
    consumer groups guarantee one consumer per pending entry) if throughput
    ever demands it."""

    def __init__(
        self,
        store: RedisMediaEvidenceStore,
        consumer_name: str,
        priorities: Sequence[str] = ("default",),
        consumer_group: str = CONSUMER_GROUP,
        block_ms: int = 5000,
        lease_ms: int = 30_000,
        reclaim_batch_size: int = 10,
    ):
        self._store = store
        # Deliberately a separate connection from `store.redis_conn` -- see
        # `_blocking_read_client`'s docstring. `complete_forwarded_
        # fingerprint_job` still goes through `self._store` (the store's
        # own connection/Lua scripts, unaffected); every raw stream/hash
        # operation this class does itself uses this one instead.
        self._redis = _blocking_read_client(store, block_ms)
        self._consumer_name = consumer_name
        self._consumer_group = consumer_group
        self._streams = [results_stream_key(priority) for priority in priorities]
        self._block_ms = block_ms
        self._lease_ms = lease_ms
        self._reclaim_batch_size = reclaim_batch_size
        self.metrics = ResultConsumerMetrics()
        self._stop = False
        for stream in self._streams:
            self._ensure_group(stream)

    def close(self) -> None:
        """Release this consumer's dedicated blocking-read connection.
        Does not touch `store` -- the caller owns that connection's
        lifecycle (its own `close()`)."""
        self._redis.close()

    def _ensure_group(self, stream: str) -> None:
        try:
            self._redis.xgroup_create(stream, self._consumer_group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def stop(self) -> None:
        self._stop = True

    def _evidence_exists(self, asset_id: str) -> bool:
        return self._redis.exists(f"{self._store.namespace}:job:{asset_id}") == 1

    def _process_entry(self, stream: str, entry_id: str, fields: dict) -> None:
        try:
            job_id = parse_result_event(fields)
        except MalformedResultEventError as exc:
            logger.warning(f"result-consumer: malformed result event entry_id={entry_id} stream={stream}: {exc}")
            self.metrics.malformed_rejected += 1
            self._redis.xack(stream, self._consumer_group, entry_id)
            return

        logger.debug(f"result-consumer: received job_id={job_id} entry_id={entry_id} stream={stream}")
        self.metrics.results_claimed += 1

        raw_record = self._redis.hgetall(result_key(job_id))
        if not raw_record:
            logger.warning(
                f"result-consumer: no durable result hash for job_id={job_id!r} (entry_id={entry_id}) -- "
                "the result event exists but its record does not; skipping, not retrying"
            )
            self.metrics.malformed_rejected += 1
            self._redis.xack(stream, self._consumer_group, entry_id)
            return

        try:
            resolved = parse_result_record(job_id, raw_record)
            result = _to_fingerprint_result(resolved)
        except MalformedResultRecordError as exc:
            logger.warning(f"result-consumer: malformed result record job_id={job_id!r}: {exc}")
            self.metrics.malformed_rejected += 1
            self._redis.xack(stream, self._consumer_group, entry_id)
            return

        if not self._evidence_exists(resolved.media_evidence_id):
            logger.warning(
                f"result-consumer: cannot resolve crawler evidence for media_evidence_id="
                f"{resolved.media_evidence_id!r} (job_id={job_id!r}) -- no evidence:job record exists; "
                "this asset can never be resolved, not retrying"
            )
            self.metrics.evidence_missing += 1
            self._redis.xack(stream, self._consumer_group, entry_id)
            return

        try:
            completed = self._store.complete_forwarded_fingerprint_job(
                resolved.media_evidence_id, fingerprint_job_id=resolved.job_id, result=result
            )
        except MediaEvidenceUnavailable:
            # Infrastructure failure, not a data/logic outcome -- do NOT ack.
            # The entry stays pending and is retried via redelivery/reclaim.
            raise

        if not completed:
            logger.warning(
                f"result-consumer: stale/duplicate completion for media_evidence_id="
                f"{resolved.media_evidence_id!r} job_id={job_id!r} -- job was not 'forwarded' under this "
                "fingerprint_job_id (already completed, or forwarded under a different job_id); no-op"
            )
            self.metrics.stale_skipped += 1
        elif resolved.decision == ResultDecision.MATCH:
            self.metrics.matches_recorded += 1
            logger.info(
                f"result-consumer: MATCH recorded media_evidence_id={resolved.media_evidence_id!r} "
                f"target={resolved.target_id}/{resolved.target_version} confidence={resolved.confidence}"
            )
        elif resolved.decision == ResultDecision.NO_MATCH:
            self.metrics.no_matches_recorded += 1
            logger.info(f"result-consumer: NO_MATCH recorded media_evidence_id={resolved.media_evidence_id!r}")
        else:
            self.metrics.failures_recorded += 1
            logger.info(
                f"result-consumer: PROCESSING_FAILURE recorded media_evidence_id={resolved.media_evidence_id!r}"
            )

        self._redis.xack(stream, self._consumer_group, entry_id)

    def process_one(self) -> bool:
        """Block for up to block_ms for new result entries across every
        configured priority stream. Returns `False` if none arrived."""
        response = self._redis.xreadgroup(
            self._consumer_group,
            self._consumer_name,
            {stream: ">" for stream in self._streams},
            count=1,
            block=self._block_ms,
        )
        if not response:
            return False
        for stream, entries in response:
            for entry_id, fields in entries:
                self._process_entry(stream, entry_id, fields)
        return True

    def reclaim_stale(self) -> int:
        """XAUTOCLAIM entries idle longer than lease_ms onto this consumer
        and process them -- mirrors `Worker.reclaim_stale` (fingerprinter
        repo). Safe to call redundantly/concurrently from every instance."""
        reclaimed = 0
        for stream in self._streams:
            _, claimed, _ = self._redis.xautoclaim(
                stream,
                self._consumer_group,
                self._consumer_name,
                min_idle_time=self._lease_ms,
                start_id="0-0",
                count=self._reclaim_batch_size,
            )
            for entry_id, fields in claimed:
                if not fields:
                    # Entry was trimmed/deleted from the stream but still
                    # lingered in the PEL; XAUTOCLAIM already dropped it there.
                    continue
                self._process_entry(stream, entry_id, fields)
                reclaimed += 1
        return reclaimed

    def run_forever(
        self,
        reclaim_interval_seconds: float = 60.0,
        idle_sleep_seconds: float = 2.0,
        deadline: Optional[float] = None,
    ) -> None:
        """Blocking loop; returns once `stop()` has been called, the
        current iteration (if any) finishes, or (if given) `deadline` -- a
        `time.monotonic()` timestamp, checked once per iteration -- has
        passed. `deadline` is a process-lifetime bound
        (`result_consumer_main.py`'s `--runtime`), never a per-result
        timeout."""
        last_reclaim = 0.0
        while not self._stop:
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("result-consumer: runtime limit elapsed, shutting down gracefully")
                self.stop()
                break
            now = time.monotonic()
            if now - last_reclaim >= reclaim_interval_seconds:
                last_reclaim = now
                try:
                    self.reclaim_stale()
                except (redis.RedisError, MediaEvidenceUnavailable) as exc:
                    logger.error(f"result-consumer: reclaim sweep failed: {exc}")

            try:
                self.process_one()
            except (redis.RedisError, MediaEvidenceUnavailable) as exc:
                logger.error(f"result-consumer: infrastructure failure, backing off: {exc}")
                time.sleep(idle_sleep_seconds)
