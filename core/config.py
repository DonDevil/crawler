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

    # Which already-registered fingerprinter TargetRegistry (target_id,
    # target_version) this crawler run is protecting -- both must be set
    # together, or both left unset (docs/architecture/
    # phase-3-target-registration-and-scoping.md). `None`/`None` (the
    # default) is a crawler run with no target scope: fingerprint jobs are
    # still created exactly as before, with no target association --
    # unchanged, pre-Phase-3 behavior. Never a default/fabricated value --
    # see core/target_scope.py.
    target_id: Optional[str] = None
    target_version: Optional[str] = None


class BridgeConfig(BaseModel):
    """Configuration for the Phase 4 crawler->fingerprinter bridge
    (docs/architecture/phase-4-crawler-fingerprinter-bridge.md) -- the
    process that forwards `evidence:jobs:queue` entries onto the
    fingerprinter's own `fingerprint:jobs:stream:{priority}` contract.

    `max_outstanding_jobs`/`submission_marker_ttl_s` mirror the
    fingerprinter's own `integration.backpressure.DEFAULT_MAX_OUTSTANDING_JOBS`
    / `integration.submission.DEFAULT_SUBMISSION_MARKER_TTL_S` defaults
    (both PROVISIONAL there, restated here for the same reason -- see that
    repo's phase-12 doc §11/§25) -- kept numerically identical so a bridge
    run with defaults behaves exactly like the fingerprinter's own
    in-process `FingerprintJobSubmitter` would, without this repo importing
    that class.
    """

    max_outstanding_jobs: int = 500
    submission_marker_ttl_s: int = 24 * 60 * 60

    # Crawler priority -> fingerprinter priority band (docs/architecture/
    # phase-4-crawler-fingerprinter-bridge.md, "Priority propagation").
    # PROVISIONAL, uncalibrated (no threshold mapping exists anywhere in
    # either repo prior to this phase -- see the fingerprinter's own
    # phase-12 doc §12, which explicitly declined to invent one and named
    # "a future bridge component" as the place it belongs). Crawler
    # priority is a plain int, lower = more urgent, conventional default
    # 10 (storage/media_evidence_store.py): priority <= priority_high_max
    # maps to the fingerprinter's HIGH stream, priority >= priority_low_min
    # maps to LOW, everything in between (including the default, 10) maps
    # to NORMAL. Operator-tunable; not load-tested.
    priority_high_max: int = 5
    priority_low_min: int = 15

    # FingerprintCandidate.max_attempts' default (fingerprinter repo,
    # integration/candidate.py) -- the bridge has no per-candidate signal
    # to vary this, so every forwarded job gets the same value.
    max_attempts: int = 3

    # How long the bridge sleeps after finding the evidence queue empty
    # before polling again.
    poll_idle_sleep_seconds: float = 2.0

    # Cadence of the bridge's own reclaim_expired_jobs() sweep, mirroring
    # CrawlerManager._recovery_loop's use of the frontier's equivalent
    # sweep (core/crawler_manager.py) -- reuses the evidence store's own
    # existing recovery mechanism, not a new one.
    reclaim_interval_seconds: float = 60.0


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


class NetworkHealthConfig(BaseModel):
    """Configuration for process-local network-outage detection (N3,
    docs/architecture/network-failure-handling-design.md §11).

    Every numeric default below is a PROVISIONAL default: N2 §11
    deliberately left exact tuning values to N3 as "a data-informed tuning
    decision, not asserted here without evidence," and no real
    outage-vs-dead-target failure-rate telemetry was available during this
    implementation phase either (see N1 §12/§13). Defaults here are chosen
    conservatively (favor not-flapping over fast detection) and are fully
    operator-overridable; see each field's comment for its specific
    reasoning.
    """

    enabled: bool = True

    # HEALTHY -> SUSPECT sensitivity (§3). Must stay well above the
    # ordinary rate of interleaved single-domain dead-target failures under
    # concurrency=25 (any *interleaved* success already resets this to
    # zero, so it only accumulates on an unbroken run of ambiguous
    # failures) while still reacting within roughly a few seconds to a
    # minute of a genuine outage.
    trigger_threshold: int = 10

    # Per-endpoint probe timeout (§4) -- short enough not to stall
    # detection, long enough that a merely-slow-but-present network isn't
    # misread as absent. Connectivity-check endpoints normally respond in
    # well under a second.
    probe_timeout_seconds: float = 5.0

    # Independent, non-crawl-target endpoints (§4): purpose-built
    # "is there Internet" connectivity-check endpoints from three
    # different providers/ASes/DNS zones, chosen so one vendor's own
    # outage can't masquerade as "the Internet is down." All are
    # hostname-based (not bare IPs), so DNS resolution is genuinely
    # exercised, matching §4's reasoning for rejecting bare-TCP probing.
    probe_endpoints: List[str] = Field(default_factory=lambda: [
        "https://www.gstatic.com/generate_204",
        "https://www.msftconnecttest.com/connecttest.txt",
        "https://captive.apple.com/hotspot-detect.html",
    ])

    # Debounce between the first and second failed probe round before
    # declaring OFFLINE (§3) -- exists specifically to reject sub-second
    # blips (Wi-Fi reassociation, a brief DNS hiccup) without waiting so
    # long that a real outage goes undetected for an unreasonable time.
    confirm_delay_seconds: float = 5.0

    # Cadence of re-probing while OFFLINE (§3) -- the only periodic
    # probing in this design. Frequent enough to detect recovery
    # reasonably fast; trivial load against high-uptime endpoints even at
    # this cadence.
    recovery_probe_interval_seconds: float = 15.0

    # Consecutive successful probe rounds required before OFFLINE ->
    # HEALTHY (§3) -- more than one, so a single flaky success right as
    # connectivity is half-restored doesn't immediately dump every worker
    # back into claiming against a still-unstable link.
    recovery_confirm_rounds: int = 2

    # Fixed (non-exponential) delay used by mark_deferred (§6) -- must be
    # nonzero to avoid a tight reclaim loop hammering Redis while still
    # offline (§14), but small since this isn't a target-failure backoff.
    deferred_requeue_delay_seconds: float = 10.0


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
    network_health: NetworkHealthConfig = NetworkHealthConfig()
    bridge: BridgeConfig = BridgeConfig()


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
