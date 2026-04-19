"""Separate SQLite storage for piracy media evidence and future hashing jobs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from utils.url_utils import URLUtils


class MediaEvidenceDatabase:
    """Persist discovered media assets, observations, and sampling jobs."""

    def __init__(self, path: str = "storage/media_evidence.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS media_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL DEFAULT 'unknown',
                source_domain TEXT,
                mime_type TEXT,
                status TEXT NOT NULL DEFAULT 'queued_for_sampling',
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                last_source_page TEXT,
                last_referrer_url TEXT,
                last_discovered_by TEXT,
                last_discovery_method TEXT,
                match_confidence REAL,
                matched_title TEXT
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
            """CREATE TABLE IF NOT EXISTS sample_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 10,
                retry_count INTEGER NOT NULL DEFAULT 0,
                byte_range_strategy TEXT NOT NULL DEFAULT 'head-window',
                claimed_by TEXT,
                last_error TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
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
        source_page: str | None = None,
        referrer_url: str | None = None,
        discovered_by: str = "unknown",
        discovery_method: str = "parser",
        media_type: str | None = None,
        mime_type: str | None = None,
        content_length: int | None = None,
        priority: int = 10,
    ) -> int:
        """Upsert a discovered media asset, log the observation, and enqueue a sample job."""

        cleaned = URLUtils.clean_media_url(url)
        if not cleaned:
            raise ValueError(f"Invalid media URL: {url}")

        normalized_media_type = media_type or URLUtils.classify_media_url(cleaned, mime_type)
        now = self._now()
        source_domain = URLUtils.extract_domain(source_page or cleaned)

        self._conn.execute(
            """INSERT INTO media_assets (
                url, media_type, source_domain, mime_type, status, first_seen, last_seen,
                last_source_page, last_referrer_url, last_discovered_by, last_discovery_method
            ) VALUES (?, ?, ?, ?, 'queued_for_sampling', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                media_type = COALESCE(excluded.media_type, media_assets.media_type),
                source_domain = COALESCE(excluded.source_domain, media_assets.source_domain),
                mime_type = COALESCE(excluded.mime_type, media_assets.mime_type),
                last_seen = excluded.last_seen,
                last_source_page = COALESCE(excluded.last_source_page, media_assets.last_source_page),
                last_referrer_url = COALESCE(excluded.last_referrer_url, media_assets.last_referrer_url),
                last_discovered_by = excluded.last_discovered_by,
                last_discovery_method = excluded.last_discovery_method,
                status = CASE
                    WHEN media_assets.status IN ('matched', 'hashed') THEN media_assets.status
                    ELSE 'queued_for_sampling'
                END""",
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

        cur = self._conn.execute("SELECT id FROM media_assets WHERE url = ?", (cleaned,))
        row = cur.fetchone()
        if row is None:
            self._conn.rollback()
            raise RuntimeError(f"Failed to resolve media asset row for {cleaned}")

        asset_id = int(row["id"])

        self._conn.execute(
            """INSERT INTO media_observations (
                asset_id, source_page, referrer_url, discovered_by,
                discovery_method, mime_type, content_length, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                asset_id,
                source_page,
                referrer_url,
                discovered_by,
                discovery_method,
                mime_type,
                content_length,
                now,
            ),
        )

        self._conn.execute(
            """INSERT INTO sample_jobs (
                asset_id, status, priority, retry_count, byte_range_strategy,
                last_error, created_at, updated_at
            ) VALUES (?, 'pending', ?, 0, 'head-window', NULL, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                priority = MIN(sample_jobs.priority, excluded.priority),
                updated_at = excluded.updated_at,
                status = CASE
                    WHEN sample_jobs.status IN ('sampled', 'hashed', 'matched') THEN sample_jobs.status
                    ELSE sample_jobs.status
                END""",
            (asset_id, priority, now, now),
        )

        self._conn.commit()
        return asset_id

    def record_manifest_variants(self, asset_id: int, variants: list[dict]) -> None:
        now = self._now()
        for variant in variants:
            variant_url = URLUtils.clean_media_url(variant.get("url", ""))
            if not variant_url:
                continue
            self._conn.execute(
                """INSERT INTO manifest_variants (
                    asset_id, variant_url, bandwidth, resolution, codecs, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id, variant_url) DO UPDATE SET
                    bandwidth = COALESCE(excluded.bandwidth, manifest_variants.bandwidth),
                    resolution = COALESCE(excluded.resolution, manifest_variants.resolution),
                    codecs = COALESCE(excluded.codecs, manifest_variants.codecs),
                    discovered_at = excluded.discovered_at""",
                (
                    asset_id,
                    variant_url,
                    variant.get("bandwidth"),
                    variant.get("resolution"),
                    variant.get("codecs"),
                    now,
                ),
            )
        self._conn.commit()

    def list_media_assets(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM media_assets ORDER BY last_seen DESC")
        return [dict(row) for row in cur.fetchall()]

    def list_observations(self, asset_id: int) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM media_observations WHERE asset_id = ? ORDER BY observed_at ASC",
            (asset_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def list_manifest_variants(self, asset_id: int) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM manifest_variants WHERE asset_id = ? ORDER BY bandwidth ASC, variant_url ASC",
            (asset_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_sample_jobs(self, statuses: Sequence[str] | None = None) -> list[dict]:
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            cur = self._conn.execute(
                f"SELECT * FROM sample_jobs WHERE status IN ({placeholders}) ORDER BY priority ASC, updated_at ASC",
                tuple(statuses),
            )
        else:
            cur = self._conn.execute("SELECT * FROM sample_jobs ORDER BY priority ASC, updated_at ASC")
        return [dict(row) for row in cur.fetchall()]

    def claim_next_sample_job(self, worker_name: str) -> dict | None:
        pending_jobs = self.get_sample_jobs(statuses=["pending"])
        if not pending_jobs:
            return None

        job = pending_jobs[0]
        asset_id = int(job["asset_id"])
        now = self._now()
        self._conn.execute(
            "UPDATE sample_jobs SET status = 'claimed', claimed_by = ?, updated_at = ? WHERE asset_id = ?",
            (worker_name, now, asset_id),
        )
        self._conn.execute(
            "UPDATE media_assets SET status = 'claimed', last_seen = ? WHERE id = ?",
            (now, asset_id),
        )
        self._conn.commit()

        refreshed = self._conn.execute("SELECT * FROM sample_jobs WHERE asset_id = ?", (asset_id,)).fetchone()
        return dict(refreshed) if refreshed else None

    def complete_sample_job(
        self,
        asset_id: int,
        *,
        fingerprint_status: str,
        match_confidence: float | None = None,
        matched_title: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = self._now()
        self._conn.execute(
            "UPDATE sample_jobs SET status = ?, last_error = ?, updated_at = ? WHERE asset_id = ?",
            (fingerprint_status, last_error, now, asset_id),
        )
        self._conn.execute(
            "UPDATE media_assets SET status = ?, last_seen = ?, match_confidence = ?, matched_title = ? WHERE id = ?",
            (fingerprint_status, now, match_confidence, matched_title, asset_id),
        )
        self._conn.commit()

    def mark_asset_matched(
        self,
        asset_id: int,
        *,
        matched_title: str,
        confidence: float,
        domain_database=None,
        score_increment: float = 1.0,
    ) -> str | None:
        self.complete_sample_job(
            asset_id,
            fingerprint_status="matched",
            match_confidence=confidence,
            matched_title=matched_title,
        )

        row = self._conn.execute(
            "SELECT source_domain FROM media_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        source_domain = row[0] if row else None

        if source_domain and domain_database is not None:
            current_score = domain_database.get_score(source_domain) or 0.0
            domain_database.add_or_update(source_domain, score=current_score + score_increment)

        return source_domain

    def update_sample_job_status(self, asset_id: int, status: str, last_error: str | None = None) -> None:
        now = self._now()
        self._conn.execute(
            "UPDATE sample_jobs SET status = ?, last_error = ?, updated_at = ? WHERE asset_id = ?",
            (status, last_error, now, asset_id),
        )
        self._conn.execute(
            "UPDATE media_assets SET status = ?, last_seen = ? WHERE id = ?",
            (status, now, asset_id),
        )
        self._conn.commit()

    def clear(self) -> None:
        self._conn.execute("DELETE FROM manifest_variants")
        self._conn.execute("DELETE FROM media_observations")
        self._conn.execute("DELETE FROM sample_jobs")
        self._conn.execute("DELETE FROM media_assets")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
