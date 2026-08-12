"""Configuration loader for the Anti-Piracy Crawler.

This module reads `config.yaml` and exposes a typed configuration object.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional


def _resolve_path(path: str, base_dir: Path) -> str:
    if not path:
        return path

    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)

    return str((base_dir / candidate).resolve())

import yaml
from pydantic import BaseModel, Field

from storage.media_evidence_store import (
    DEFAULT_FINGERPRINT_LEASE_TTL,
    DEFAULT_MAX_OBSERVATIONS_PER_ASSET,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_VARIANTS_PER_ASSET,
)


class StorageConfig(BaseModel):
    sqlite_path: str = "storage/crawl_state.db"
    media_sqlite_path: str = "storage/media_evidence.db"
    enable_media_evidence: bool = True
    enqueue_media_jobs: bool = True


class MediaEvidenceConfig(BaseModel):
    """Configuration for the media evidence storage/coordination backend.

    Mirrors `FrontierConfig`'s shape (`type: sqlite|redis`, its own Redis
    connection/namespace settings) but is fully independent -- see
    docs/architecture/media-evidence-redis-design.md, "Architecture
    Boundaries": there is no fallback from redis to sqlite, and the two
    backends never share state. `storage.sqlite_path`/`enable_media_evidence`
    above are unchanged and still control the SQLite file path and the
    overall on/off toggle; `type` here only selects which backend is used
    when media evidence is enabled.
    """

    type: str = "sqlite"  # 'sqlite' or 'redis'
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_namespace: str = "evidence"

    # §4/§10: bounded retention -- configurable, not hardcoded throughout
    # the store implementations.
    max_observations_per_asset: int = DEFAULT_MAX_OBSERVATIONS_PER_ASSET
    max_variants_per_asset: int = DEFAULT_MAX_VARIANTS_PER_ASSET

    # Fingerprint job lease/heartbeat (§7) -- an order of magnitude longer
    # than the frontier's URL-fetch lease_ttl (90s) because fingerprinting
    # is minutes, not milliseconds. `fingerprint_heartbeat_interval` left
    # as None auto-derives a safe interval from fingerprint_lease_ttl
    # (core.claim_heartbeat.default_heartbeat_interval), same convention as
    # FrontierConfig.heartbeat_interval -- always clamped below the lease
    # regardless of what's configured here.
    fingerprint_lease_ttl: float = DEFAULT_FINGERPRINT_LEASE_TTL
    fingerprint_heartbeat_interval: Optional[float] = None

    # §8: retry/backoff, deliberately a smaller default budget than the
    # frontier's (3) given fingerprinting's higher per-attempt cost.
    max_retries: int = DEFAULT_MAX_RETRIES
    base_backoff: float = 5.0
    max_backoff: float = 300.0

    # Redis-only: reclaim sweep batch size (§6) and confirmed_match event
    # stream trim length (§13/§19). Meaningless for the SQLite backend.
    reclaim_batch_size: int = 200
    confirmed_match_stream_maxlen: int = 10000


class FrontierConfig(BaseModel):
    """Configuration for URL frontier backend.

    Supports two modes:
    - 'sqlite': In-memory frontier with SQLite persistence (single worker)
    - 'redis': Redis-backed shared frontier (multi-worker, requires Redis server)

    Claim/retry/recovery knobs (docs/architecture/frontier-adr.md §4/§7)
    apply to both backends uniformly -- the local frontier already enforces
    max_retries/backoff (Step 1); the Redis frontier's claim lease and
    recovery sweep additionally use lease_ttl/recovery_interval/
    reclaim_batch_size/domain_scan_limit (Step 3-4).
    """
    type: str = "sqlite"  # 'sqlite' or 'redis'
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_namespace: str = "crawler"

    # Shared retry/backoff (applied uniformly by whichever frontier is active)
    max_retries: int = 3
    base_backoff: float = 5.0
    max_backoff: float = 300.0

    # Redis claim lease + background recovery sweep. Local frontier accepts
    # lease_ttl for FrontierClaim.lease_expires_at bookkeeping but performs
    # no lease-expiry logic in-process (ADR §10); recovery_enabled/
    # recovery_interval/reclaim_batch_size are meaningful only for backends
    # that implement reclaim_and_promote (Redis today).
    lease_ttl: float = 90.0
    recovery_enabled: bool = True
    recovery_interval: float = 30.0
    reclaim_batch_size: int = 200

    # One-shot startup recovery sweep (docs/architecture/history/
    # redis-startup-recovery.md): reconciles whatever a *previous* process
    # left behind (abandoned inflight claims, due retries) before this
    # process's crawler workers are allowed to start claiming, using the
    # same `reclaim_and_promote`/`recovery_enabled` gate as the periodic
    # loop above. Bounded by BOTH knobs together, not either alone, so a
    # continuously-refreshed backlog (e.g. other independent systems still
    # expiring claims into the same namespace) can't delay startup
    # indefinitely: 50 passes covers the worst backlog actually observed
    # (228 inflight + 73 retries needed 2 passes at the default batch size
    # of 200) many times over, and 30s is a hard wall-clock ceiling
    # independent of pass count or Redis latency.
    startup_recovery_max_passes: int = 50
    startup_recovery_max_duration: float = 30.0

    # Max candidate domains claim_next examines per call (Redis-only `K`
    # bound -- meaningless to the local frontier, which scans unbounded).
    # Raised from 50 to 250 (2026-08-10, see
    # docs/architecture/domain-scan-limit-decision.md): this is a bounded-
    # work safety/performance parameter, not a guarantee that every active
    # domain is globally visible -- a domain ranked outside the top K is
    # invisible to scheduling regardless of how long it's waited (see
    # docs/architecture/domain-scan-window-design.md). Do not raise this
    # indefinitely as active-domain counts grow; the eligible-domain-index
    # design documented there is the intended fix once real telemetry shows
    # active domains regularly approaching/exceeding this value.
    domain_scan_limit: int = 250

    # Claim heartbeat (docs/architecture/frontier-adr.md §8, Step 5): lets a
    # worker that's still legitimately fetching a URL renew its claim so it
    # isn't reclaimed as if it had crashed. None (the default) derives a safe
    # interval from lease_ttl automatically (core.claim_heartbeat.
    # default_heartbeat_interval) -- only set this explicitly to override
    # that default, and note it is always clamped below lease_ttl regardless
    # of what's configured here (a heartbeat interval >= lease_ttl would let
    # the lease expire before the first renewal could ever land).
    heartbeat_interval: Optional[float] = None


class SearchConfig(BaseModel):
    enabled_engines: List[str] = Field(default_factory=lambda: [
        "duckduckgo",
        "bing",
        "brave",
        "yandex",
        "ahmia",
        "torch",
    ])
    max_results_per_engine: int = 20
    timeout: int = 15
    engine_priorities: dict[str, int] = Field(default_factory=lambda: {
        "torch": 0,
        "ahmia": 2,
        "brave": 4,
        "bing": 5,
        "duckduckgo": 6,
        "yandex": 7,
    })
    onion_priority_boost: int = 2
    blocked_engine_cooldown_queries: int = 999


class CrawlerConfig(BaseModel):
    engine: str = "auto"
    concurrency: int = 25
    timeout: int = 15
    max_pages: Optional[int] = 500
    rate_limit: float = 1.0
    user_agent: Optional[str] = None
    scrapling_enabled: bool = True
    scrapling_headless: bool = True
    scrapling_stealth: bool = True
    scrapling_network_idle: bool = True
    seed_files: List[str] = Field(default_factory=lambda: [
        "seeds/piracy_sites.txt",
        "seeds/torrent_sites.txt",
        "seeds/streaming_sites.txt",
        "seeds/darkweb_seeds.txt",
    ])
    storage: StorageConfig = StorageConfig()
    frontier: FrontierConfig = FrontierConfig()
    media_evidence: MediaEvidenceConfig = MediaEvidenceConfig()


class Config(BaseModel):
    crawler: CrawlerConfig = CrawlerConfig()
    search: SearchConfig = SearchConfig()


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from YAML file.

    If the file is missing, returns defaults.
    """

    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        return Config()

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = Config(**raw)
    base_dir = config_path.parent

    if config.crawler.storage.sqlite_path:
        config.crawler.storage.sqlite_path = _resolve_path(config.crawler.storage.sqlite_path, base_dir)
    if config.crawler.storage.media_sqlite_path:
        config.crawler.storage.media_sqlite_path = _resolve_path(config.crawler.storage.media_sqlite_path, base_dir)

    return config
