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


class StorageConfig(BaseModel):
    sqlite_path: str = "storage/crawl_state.db"
    media_sqlite_path: str = "storage/media_evidence.db"
    enable_media_evidence: bool = True
    enqueue_media_jobs: bool = True


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
    domain_scan_limit: int = 50

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
