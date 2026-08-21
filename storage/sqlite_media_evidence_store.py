"""SQLite media evidence backend -- development/testing/local experimentation only.

Independent, standalone implementation of `storage.media_evidence_store.
MediaEvidenceStore`. Not a staging step toward Redis, not a fallback path,
not exercised in production -- see
docs/architecture/media-evidence-redis-design.md, "Architecture Boundaries".
`RedisMediaEvidenceStore` (storage/redis_media_evidence_store.py) is the
sole production backend; nothing here is synchronized with it.

This is a rename-and-harden of the original `MediaEvidenceDatabase`: same
tables in spirit, same single-machine dev/test scope, same unbounded
observation history (§4: the retention cap is a Redis-specific mitigation,
not a preserved SQLite behavior). What changed is the job lifecycle
(queued/claimed/completed/retry_scheduled/permanent_failure, replacing the
never-fully-wired pending/claimed/sampled/hashed/matched states -- see §5a),
token-guarded claim/renew/complete/fail (closing the "any caller can
complete any job" bug), and deriving asset status from job/result state
instead of storing it as a second, independently-writable field (closing
the desync bug documented in §1a finding (b)).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from core.target_scope import TargetScope
from storage.async_database_writer import BatchedDatabaseWriter
from storage.media_evidence_store import (
    DEFAULT_FINGERPRINT_LEASE_TTL,
    DEFAULT_MAX_RETRIES,
    FingerprintJob,
    FingerprintResult,
    InvalidMediaURLError,
    JOB_CLAIMED,
    JOB_COMPLETED,
    JOB_FORWARDED,
    JOB_PERMANENT_FAILURE,
    JOB_QUEUED,
    JOB_RETRY_SCHEDULED,
    JOB_STATUSES,
    derive_asset_status,
    truncate_metadata,
    validate_media_url_length,
)
from utils.url_utils import URLUtils


class SQLiteMediaEvidenceStore:
    """Persist discovered media assets, observations, and fingerprint jobs
    in a local SQLite file. See module docstring for scope."""

    def __init__(
        self,
        path: str = "storage/media_evidence.db",
        batch_size: int = 50,
        fingerprint_lease_ttl: float = DEFAULT_FINGERPRINT_LEASE_TTL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        target_scope: Optional[TargetScope] = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA cache_size=10000")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._writer = BatchedDatabaseWriter(self._conn, batch_size=batch_size)
        self._fingerprint_lease_ttl = fingerprint_lease_ttl
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        # docs/architecture/phase-3-target-registration-and-scoping.md --
        # see RedisMediaEvidenceStore's identical attribute.
        self.target_scope = target_scope
        # Guards asset/job creation and every job-state transition. Cheap
        # and sufficient for this backend's dev/test scope (§22 explicitly
        # scopes multi-process distributed-claim-safety testing to Redis
        # only) -- this only needs to make in-process concurrent callers
        # (e.g. threaded unit tests) safe, not survive a process crash.
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS media_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL DEFAULT 'unknown',
                source_domain TEXT,
                mime_type TEXT,
                content_id TEXT,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                last_source_page TEXT,
                last_referrer_url TEXT,
                last_discovered_by TEXT,
                last_discovery_method TEXT,
                observation_count INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS media_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                source_page TEXT,
                referrer_url TEXT,
                discovered_by TEXT,
                discovery_method TEXT,
                mime_type TEXT,
                content_length INTEGER,
                observed_at TIMESTAMP,
                FOREIGN KEY(asset_id) REFERENCES media_assets(id) ON DELETE CASCADE
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fingerprint_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 10,
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_class TEXT,
                last_error TEXT,
                claim_token TEXT,
                claimed_by TEXT,
                lease_expires_at REAL,
                retry_eligible_at REAL,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                target_id TEXT,
                target_version TEXT,
                fingerprint_job_id TEXT,
                FOREIGN KEY(asset_id) REFERENCES media_assets(id) ON DELETE CASCADE
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fingerprint_results (
                asset_id INTEGER PRIMARY KEY,
                dinov2_similarity REAL,
                phash_score REAL,
                audio_score REAL,
                temporal_verified INTEGER,
                aggregate_decision TEXT NOT NULL,
                confidence REAL,
                algorithm_versions TEXT,
                worker_id TEXT,
                matched_title TEXT,
                processed_at TIMESTAMP,
                FOREIGN KEY(asset_id) REFERENCES media_assets(id) ON DELETE CASCADE
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS manifest_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                variant_url TEXT NOT NULL,
                bandwidth INTEGER,
                resolution TEXT,
                codecs TEXT,
                discovered_at TIMESTAMP,
                UNIQUE(asset_id, variant_url),
                FOREIGN KEY(asset_id) REFERENCES media_assets(id) ON DELETE CASCADE
            )"""
        )
        self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

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
        """Upsert a discovered media asset, log the observation, and enqueue
        a fingerprint job on first discovery only. See
        docs/architecture/media-evidence-redis-design.md §1a for the exact
        semantics this preserves."""

        cleaned = URLUtils.clean_media_url(url)
        if not cleaned:
            raise InvalidMediaURLError(f"Invalid media URL: {url}")
        validate_media_url_length(cleaned)

        normalized_media_type = media_type or URLUtils.classify_media_url(cleaned, mime_type)
        mime_type = truncate_metadata(mime_type)
        now = self._now()
        source_domain = URLUtils.extract_domain(source_page or cleaned)

        with self._lock:
            self._writer.execute(
                """INSERT INTO media_assets (
                    url, media_type, source_domain, mime_type, first_seen, last_seen,
                    last_source_page, last_referrer_url, last_discovered_by, last_discovery_method,
                    observation_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(url) DO UPDATE SET
                    media_type = COALESCE(excluded.media_type, media_assets.media_type),
                    source_domain = COALESCE(excluded.source_domain, media_assets.source_domain),
                    mime_type = COALESCE(excluded.mime_type, media_assets.mime_type),
                    last_seen = excluded.last_seen,
                    last_source_page = COALESCE(excluded.last_source_page, media_assets.last_source_page),
                    last_referrer_url = COALESCE(excluded.last_referrer_url, media_assets.last_referrer_url),
                    last_discovered_by = excluded.last_discovered_by,
                    last_discovery_method = excluded.last_discovery_method,
                    observation_count = media_assets.observation_count + 1""",
                (
                    cleaned,
                    normalized_media_type,
                    source_domain,
                    mime_type,
                    now,
                    now,
                    source_page,
                    referrer_url,
                    discovered_by,
                    discovery_method,
                ),
            )

            row = self._conn.execute("SELECT id FROM media_assets WHERE url = ?", (cleaned,)).fetchone()
            if row is None:
                raise RuntimeError(f"Failed to resolve media asset row for {cleaned}")
            asset_id = int(row["id"])

            self._writer.execute(
                """INSERT INTO media_observations (
                    asset_id, source_page, referrer_url, discovered_by,
                    discovery_method, mime_type, content_length, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (asset_id, source_page, referrer_url, discovered_by, discovery_method, mime_type, content_length, now),
            )

            target_id = self.target_scope.target_id if self.target_scope else None
            target_version = self.target_scope.target_version if self.target_scope else None
            self._writer.execute(
                """INSERT INTO fingerprint_jobs (
                    asset_id, status, priority, retry_count, created_at, updated_at,
                    target_id, target_version
                ) VALUES (?, 'queued', ?, 0, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    priority = MIN(fingerprint_jobs.priority, excluded.priority),
                    updated_at = excluded.updated_at""",
                (asset_id, priority, now, now, target_id, target_version),
            )

        return str(asset_id)

    def record_manifest_variants(self, asset_id: str, variants: list[dict]) -> None:
        """Attach descriptive manifest variant metadata (§10) -- unbounded
        here (dev/test scope; Redis enforces the abuse-resistant cap)."""
        aid = int(asset_id)
        now = self._now()
        for variant in variants:
            variant_url = URLUtils.clean_media_url(variant.get("url", ""))
            if not variant_url:
                continue
            self._writer.execute(
                """INSERT INTO manifest_variants (
                    asset_id, variant_url, bandwidth, resolution, codecs, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id, variant_url) DO UPDATE SET
                    bandwidth = COALESCE(excluded.bandwidth, manifest_variants.bandwidth),
                    resolution = COALESCE(excluded.resolution, manifest_variants.resolution),
                    codecs = COALESCE(excluded.codecs, manifest_variants.codecs),
                    discovered_at = excluded.discovered_at""",
                (aid, variant_url, variant.get("bandwidth"), variant.get("resolution"), variant.get("codecs"), now),
            )

    def list_media_assets(self, *, limit: Optional[int] = None) -> list[dict]:
        """Return known assets, most recently seen first, with `status`
        derived from job/result state (§2) rather than read from a
        separately-writable column."""
        query = """
            SELECT a.*, j.status AS job_status, r.aggregate_decision, r.confidence AS match_confidence,
                   r.matched_title
            FROM media_assets a
            LEFT JOIN fingerprint_jobs j ON j.asset_id = a.id
            LEFT JOIN fingerprint_results r ON r.asset_id = a.id
            ORDER BY a.last_seen DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            cur = self._conn.execute(query, (limit,))
        else:
            cur = self._conn.execute(query)

        assets = []
        for row in cur.fetchall():
            record = dict(row)
            job_status = record.pop("job_status") or JOB_QUEUED
            aggregate_decision = record.pop("aggregate_decision")
            record["id"] = str(record["id"])
            record["status"] = derive_asset_status(job_status, aggregate_decision)
            assets.append(record)
        return assets

    def list_observations(self, asset_id: str) -> list[dict]:
        """Full, unbounded observation history (§4 -- never capped for this
        dev/test backend)."""
        cur = self._conn.execute(
            "SELECT * FROM media_observations WHERE asset_id = ? ORDER BY observed_at ASC",
            (int(asset_id),),
        )
        return [dict(row) for row in cur.fetchall()]

    def list_manifest_variants(self, asset_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM manifest_variants WHERE asset_id = ? ORDER BY bandwidth ASC, variant_url ASC",
            (int(asset_id),),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_fingerprint_jobs(self, statuses: Optional[Sequence[str]] = None) -> list[dict]:
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            cur = self._conn.execute(
                f"SELECT * FROM fingerprint_jobs WHERE status IN ({placeholders}) "
                "ORDER BY priority ASC, updated_at ASC",
                tuple(statuses),
            )
        else:
            cur = self._conn.execute("SELECT * FROM fingerprint_jobs ORDER BY priority ASC, updated_at ASC")

        jobs = []
        for row in cur.fetchall():
            record = dict(row)
            record["asset_id"] = str(record["asset_id"])
            jobs.append(record)
        return jobs

    def claim_next_fingerprint_job(self, worker_id: str) -> Optional[FingerprintJob]:
        """Atomically (within this process) claim the highest-priority
        queued job. See module docstring for this backend's concurrency
        scope."""
        with self._lock:
            row = self._conn.execute(
                "SELECT asset_id, priority, retry_count, target_id, target_version FROM fingerprint_jobs "
                "WHERE status = ? ORDER BY priority ASC, updated_at ASC LIMIT 1",
                (JOB_QUEUED,),
            ).fetchone()
            if row is None:
                return None

            asset_id = int(row["asset_id"])
            token = uuid.uuid4().hex
            now_epoch = time.time()
            lease_expires_at = now_epoch + self._fingerprint_lease_ttl

            self._writer.execute(
                """UPDATE fingerprint_jobs
                   SET status = ?, claim_token = ?, claimed_by = ?, lease_expires_at = ?, updated_at = ?
                   WHERE asset_id = ? AND status = ?""",
                (JOB_CLAIMED, token, worker_id, lease_expires_at, self._now(), asset_id, JOB_QUEUED),
            )

            asset_row = self._conn.execute(
                "SELECT url, media_type FROM media_assets WHERE id = ?", (asset_id,)
            ).fetchone()

            return FingerprintJob(
                asset_id=str(asset_id),
                token=token,
                canonical_url=asset_row["url"],
                media_type=asset_row["media_type"],
                priority=int(row["priority"]),
                retry_count=int(row["retry_count"]),
                lease_expires_at=lease_expires_at,
                target_id=row["target_id"],
                target_version=row["target_version"],
            )

    def _current_claim(self, aid: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT status, claim_token, retry_count, priority FROM fingerprint_jobs WHERE asset_id = ?",
            (aid,),
        ).fetchone()

    def renew_job_lease(self, asset_id: str, token: str) -> bool:
        aid = int(asset_id)
        with self._lock:
            claim = self._current_claim(aid)
            if claim is None or claim["status"] != JOB_CLAIMED or claim["claim_token"] != token:
                return False

            lease_expires_at = time.time() + self._fingerprint_lease_ttl
            self._writer.execute(
                "UPDATE fingerprint_jobs SET lease_expires_at = ?, updated_at = ? WHERE asset_id = ?",
                (lease_expires_at, self._now(), aid),
            )
            return True

    def complete_fingerprint_job(self, asset_id: str, token: str, *, result: FingerprintResult) -> bool:
        aid = int(asset_id)
        with self._lock:
            claim = self._current_claim(aid)
            if claim is None or claim["status"] != JOB_CLAIMED or claim["claim_token"] != token:
                return False

            now = self._now()
            self._writer.execute(
                "UPDATE fingerprint_jobs SET status = ?, claim_token = NULL, updated_at = ? WHERE asset_id = ?",
                (JOB_COMPLETED, now, aid),
            )
            self._writer.execute(
                """INSERT INTO fingerprint_results (
                    asset_id, dinov2_similarity, phash_score, audio_score, temporal_verified,
                    aggregate_decision, confidence, algorithm_versions, worker_id, matched_title, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    dinov2_similarity = excluded.dinov2_similarity,
                    phash_score = excluded.phash_score,
                    audio_score = excluded.audio_score,
                    temporal_verified = excluded.temporal_verified,
                    aggregate_decision = excluded.aggregate_decision,
                    confidence = excluded.confidence,
                    algorithm_versions = excluded.algorithm_versions,
                    worker_id = excluded.worker_id,
                    matched_title = excluded.matched_title,
                    processed_at = excluded.processed_at""",
                (
                    aid,
                    result.dinov2_similarity,
                    result.phash_score,
                    result.audio_score,
                    None if result.temporal_verified is None else int(result.temporal_verified),
                    result.aggregate_decision,
                    result.confidence,
                    json.dumps(result.algorithm_versions) if result.algorithm_versions else None,
                    result.worker_id,
                    truncate_metadata(result.matched_title),
                    result.processed_at or now,
                ),
            )
            return True

    def fail_fingerprint_job(
        self,
        asset_id: str,
        token: str,
        *,
        error_class: str,
        last_error: str,
        retryable: bool,
    ) -> bool:
        aid = int(asset_id)
        with self._lock:
            claim = self._current_claim(aid)
            if claim is None or claim["status"] != JOB_CLAIMED or claim["claim_token"] != token:
                return False

            error_class = truncate_metadata(error_class) or ""
            last_error = truncate_metadata(last_error) or ""
            new_retry_count = int(claim["retry_count"]) + 1
            now = self._now()

            if retryable and new_retry_count < self._max_retries:
                backoff = min(self._base_backoff * (2 ** (new_retry_count - 1)), self._max_backoff)
                self._writer.execute(
                    """UPDATE fingerprint_jobs
                       SET status = ?, retry_count = ?, error_class = ?, last_error = ?,
                           claim_token = NULL, retry_eligible_at = ?, updated_at = ?
                       WHERE asset_id = ?""",
                    (JOB_RETRY_SCHEDULED, new_retry_count, error_class, last_error, time.time() + backoff, now, aid),
                )
            else:
                self._writer.execute(
                    """UPDATE fingerprint_jobs
                       SET status = ?, retry_count = ?, error_class = ?, last_error = ?,
                           claim_token = NULL, updated_at = ?
                       WHERE asset_id = ?""",
                    (JOB_PERMANENT_FAILURE, new_retry_count, error_class, last_error, now, aid),
                )
            return True

    def mark_fingerprint_job_forwarded(self, asset_id: str, token: str, *, fingerprint_job_id: str) -> bool:
        """SQLite-backend parity with `RedisMediaEvidenceStore`'s identical
        method -- see `JOB_FORWARDED`'s docstring (storage/media_evidence_
        store.py) for why this is distinct from `complete_fingerprint_job`."""
        aid = int(asset_id)
        with self._lock:
            claim = self._current_claim(aid)
            if claim is None or claim["status"] != JOB_CLAIMED or claim["claim_token"] != token:
                return False

            now = self._now()
            self._writer.execute(
                """UPDATE fingerprint_jobs
                   SET status = ?, claim_token = NULL, fingerprint_job_id = ?, updated_at = ?
                   WHERE asset_id = ?""",
                (JOB_FORWARDED, fingerprint_job_id, now, aid),
            )
            return True

    def reclaim_expired_jobs(self, batch_size: int = 200) -> tuple[int, int]:
        """Reclaim leases that expired without renewal/completion (treated
        as a recoverable worker crash, §8), and promote due
        `retry_scheduled` jobs back to `queued` (§6)."""
        with self._lock:
            now_epoch = time.time()
            now_iso = self._now()

            expired = self._conn.execute(
                "SELECT asset_id, retry_count FROM fingerprint_jobs "
                "WHERE status = ? AND lease_expires_at < ? LIMIT ?",
                (JOB_CLAIMED, now_epoch, batch_size),
            ).fetchall()

            reclaimed = 0
            for row in expired:
                aid = int(row["asset_id"])
                new_retry_count = int(row["retry_count"]) + 1
                if new_retry_count < self._max_retries:
                    backoff = min(self._base_backoff * (2 ** (new_retry_count - 1)), self._max_backoff)
                    self._writer.execute(
                        """UPDATE fingerprint_jobs
                           SET status = ?, retry_count = ?, claim_token = NULL,
                               retry_eligible_at = ?, updated_at = ? WHERE asset_id = ?""",
                        (JOB_RETRY_SCHEDULED, new_retry_count, now_epoch + backoff, now_iso, aid),
                    )
                else:
                    self._writer.execute(
                        """UPDATE fingerprint_jobs
                           SET status = ?, retry_count = ?, claim_token = NULL, updated_at = ?
                           WHERE asset_id = ?""",
                        (JOB_PERMANENT_FAILURE, new_retry_count, now_iso, aid),
                    )
                reclaimed += 1

            due = self._conn.execute(
                "SELECT asset_id FROM fingerprint_jobs WHERE status = ? AND retry_eligible_at < ? LIMIT ?",
                (JOB_RETRY_SCHEDULED, now_epoch, batch_size),
            ).fetchall()

            requeued = 0
            for row in due:
                self._writer.execute(
                    "UPDATE fingerprint_jobs SET status = ?, updated_at = ? WHERE asset_id = ?",
                    (JOB_QUEUED, now_iso, int(row["asset_id"])),
                )
                requeued += 1

            return reclaimed, requeued

    def get_status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in JOB_STATUSES}
        cur = self._conn.execute("SELECT status, COUNT(*) AS n FROM fingerprint_jobs GROUP BY status")
        for row in cur.fetchall():
            if row["status"] in counts:
                counts[row["status"]] = int(row["n"])
        return counts

    def clear(self) -> None:
        with self._lock:
            self._writer.execute("DELETE FROM manifest_variants")
            self._writer.execute("DELETE FROM fingerprint_results")
            self._writer.execute("DELETE FROM fingerprint_jobs")
            self._writer.execute("DELETE FROM media_observations")
            self._writer.execute("DELETE FROM media_assets")
            self._writer.flush()

    def close(self) -> None:
        self._writer.flush()
        self._conn.close()
