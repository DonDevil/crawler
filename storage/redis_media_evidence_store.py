"""Redis-backed, fleet-wide media evidence store for production use.

Implements `storage.media_evidence_store.MediaEvidenceStore` against the
Redis keyspace and claim/lease design in
docs/architecture/media-evidence-redis-design.md (§6-§14) and
docs/architecture/media-evidence-step1.md. `RedisURLFrontier`
(core/redis_frontier.py) is the proven-pattern reference this backend
reuses (atomic Lua claim, CAS ownership token, lease + reclaim sweep) --
deliberately *not* copied is the frontier's per-domain fairness/scan-window
machinery, which solves a problem fingerprint jobs don't have (§6): a
single global priority queue is sufficient here.

Every state-changing operation is a single Lua script (one Redis round
trip). The evidence keyspace (`media_evidence.redis_namespace`, default
"evidence") is fully separate from the frontier's own namespace (default
"crawler") -- see the Architecture Boundaries section of the design doc:
no synchronization, no fallback, no mirroring to/from SQLite in either
direction.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional, Sequence

import redis
from loguru import logger

from core.target_scope import TargetScope
from storage.media_evidence_store import (
    DEFAULT_FINGERPRINT_LEASE_TTL,
    DEFAULT_MAX_OBSERVATIONS_PER_ASSET,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_VARIANTS_PER_ASSET,
    FingerprintJob,
    FingerprintResult,
    InvalidMediaURLError,
    JOB_CLAIMED,
    JOB_COMPLETED,
    JOB_FORWARDED,
    JOB_PERMANENT_FAILURE,
    JOB_QUEUED,
    JOB_RETRY_SCHEDULED,
    MediaEvidenceUnavailable,
    compute_discovery_id,
    derive_asset_status,
    truncate_metadata,
    validate_media_url_length,
)
from utils.url_utils import URLUtils

# Score formula for the global job queue: priority is the major sort key, a
# monotonic sequence number breaks ties in insertion order -- identical
# convention to core/redis_frontier.py's `_SEQ_SCORE_SCALE`.
_SEQ_SCORE_SCALE = 1_000_000

# Status -> (structure kind, key suffix) for the indices `get_fingerprint_jobs`
# can scan without a full keyspace SCAN. `completed` has no such index on
# purpose -- job hashes are discardable operational state after completion
# (§12/§14); durable evidence lives in `{ns}:result:{aid}` instead.
_STATUS_INDEX = {
    JOB_QUEUED: ("zset", ("jobs", "queue")),
    JOB_CLAIMED: ("zset", ("jobs", "inflight")),
    JOB_RETRY_SCHEDULED: ("zset", ("jobs", "retry_scheduled")),
    JOB_PERMANENT_FAILURE: ("set", ("jobs", "permanent_failure")),
}


def _to_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _to_optional_bool(value: Optional[str]) -> Optional[bool]:
    if value is None or value == "":
        return None
    return value == "true"


class RedisMediaEvidenceStore:
    """Distributed media evidence store using Redis for fleet-wide
    coordination. See module docstring for the design this implements."""

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        namespace: str = "evidence",
        max_observations_per_asset: int = DEFAULT_MAX_OBSERVATIONS_PER_ASSET,
        max_variants_per_asset: int = DEFAULT_MAX_VARIANTS_PER_ASSET,
        fingerprint_lease_ttl: float = DEFAULT_FINGERPRINT_LEASE_TTL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        reclaim_batch_size: int = 200,
        confirmed_match_stream_maxlen: int = 10000,
        target_scope: Optional[TargetScope] = None,
    ):
        """Connect and register Lua scripts. Raises `redis.ConnectionError`
        if the server is unreachable -- callers (e.g. `CrawlerManager`) must
        not catch this and silently fall back to SQLite: a production Redis
        outage must be visible as a media-evidence availability failure,
        not degrade to a different backend (docs/architecture/
        media-evidence-step1.md, "Backend selection")."""
        self.namespace = namespace
        self.max_observations_per_asset = max_observations_per_asset
        self.max_variants_per_asset = max_variants_per_asset
        self.fingerprint_lease_ttl = fingerprint_lease_ttl
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.reclaim_batch_size = reclaim_batch_size
        self.confirmed_match_stream_maxlen = confirmed_match_stream_maxlen
        # docs/architecture/phase-3-target-registration-and-scoping.md:
        # which already-registered fingerprinter target this run's jobs are
        # associated with, or `None` for an unscoped run (jobs get no
        # target association, exactly this store's pre-Phase-3 behavior).
        self.target_scope = target_scope

        self.redis_conn = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            socket_keepalive=True,
            health_check_interval=30,
            decode_responses=True,
        )
        self.redis_conn.ping()
        logger.info(f"Connected to media evidence Redis at {redis_host}:{redis_port}/{redis_db} ns={namespace}")

        self._init_lua_scripts()

    def _key(self, *parts: str) -> str:
        return f"{self.namespace}:" + ":".join(parts)

    # ------------------------------------------------------------------
    # Lua scripts -- every mutation is exactly one of these (one Redis
    # round trip). See each public method's docstring for the
    # round-trip/complexity accounting and docs/architecture/
    # media-evidence-step1.md for the full atomicity/invariant writeup.
    # ------------------------------------------------------------------

    def _init_lua_scripts(self) -> None:
        self._record_media_link_script = self.redis_conn.register_script(
            """
            -- Atomically upserts one asset, appends+caps one observation,
            -- and creates a fingerprint job iff this is the asset's
            -- first-ever discovery (never re-created, never re-triggered
            -- by rediscovery of a terminal asset -- job *status* is never
            -- touched here under any condition). Invariant: exactly one
            -- job per asset, for the asset's whole lifetime.
            local aid = ARGV[1]
            local url = ARGV[2]
            local media_type = ARGV[3]
            local mime_type = ARGV[4]
            local source_domain = ARGV[5]
            local source_page = ARGV[6]
            local referrer_url = ARGV[7]
            local discovered_by = ARGV[8]
            local discovery_method = ARGV[9]
            local content_length = ARGV[10]
            local priority = tonumber(ARGV[11])
            local max_observations = tonumber(ARGV[12])
            local ns = ARGV[13]
            local target_id = ARGV[14]
            local target_version = ARGV[15]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local asset_key = ns .. ':asset:' .. aid
            local is_new = redis.call('EXISTS', asset_key) == 0

            if is_new then
                redis.call('HSET', asset_key,
                    'canonical_url', url,
                    'media_type', media_type,
                    'source_domain', source_domain,
                    'mime_type', mime_type,
                    'first_seen', tostring(now),
                    'last_seen', tostring(now),
                    'last_source_page', source_page,
                    'last_referrer_url', referrer_url,
                    'last_discovered_by', discovered_by,
                    'last_discovery_method', discovery_method,
                    'observation_count', '0')
            else
                redis.call('HSET', asset_key, 'last_seen', tostring(now))
                if media_type ~= '' then redis.call('HSET', asset_key, 'media_type', media_type) end
                if source_domain ~= '' then redis.call('HSET', asset_key, 'source_domain', source_domain) end
                if mime_type ~= '' then redis.call('HSET', asset_key, 'mime_type', mime_type) end
                if source_page ~= '' then redis.call('HSET', asset_key, 'last_source_page', source_page) end
                if referrer_url ~= '' then redis.call('HSET', asset_key, 'last_referrer_url', referrer_url) end
                redis.call('HSET', asset_key, 'last_discovered_by', discovered_by)
                redis.call('HSET', asset_key, 'last_discovery_method', discovery_method)
            end

            redis.call('ZADD', ns .. ':assets:all', now, aid)
            redis.call('HINCRBY', asset_key, 'observation_count', 1)

            local observation = cjson.encode({
                source_page = source_page,
                referrer_url = referrer_url,
                discovered_by = discovered_by,
                discovery_method = discovery_method,
                mime_type = mime_type,
                content_length = content_length,
                observed_at = now,
            })
            local obs_key = ns .. ':asset:' .. aid .. ':observations'
            redis.call('LPUSH', obs_key, observation)
            redis.call('LTRIM', obs_key, 0, max_observations - 1)

            local job_key = ns .. ':job:' .. aid
            if redis.call('EXISTS', job_key) == 0 then
                redis.call('HSET', job_key,
                    'status', 'queued',
                    'priority', tostring(priority),
                    'retry_count', '0',
                    'error_class', '',
                    'last_error', '',
                    'created_at', tostring(now),
                    'updated_at', tostring(now),
                    'target_id', target_id,
                    'target_version', target_version)
                local seq = redis.call('INCR', ns .. ':jobs:seq')
                local score = priority * %(scale)d + seq
                redis.call('ZADD', ns .. ':jobs:queue', score, aid)
            else
                local current_priority = tonumber(redis.call('HGET', job_key, 'priority'))
                if priority < current_priority then
                    redis.call('HSET', job_key, 'priority', tostring(priority), 'updated_at', tostring(now))
                    if redis.call('HGET', job_key, 'status') == 'queued' then
                        local seq = redis.call('INCR', ns .. ':jobs:seq')
                        local score = priority * %(scale)d + seq
                        redis.call('ZADD', ns .. ':jobs:queue', score, aid)
                    end
                end
            end

            return is_new and 1 or 0
            """
            % {"scale": _SEQ_SCORE_SCALE}
        )

        self._record_variants_script = self.redis_conn.register_script(
            """
            -- Bounded manifest-variant metadata (§10/§16): once
            -- `max_variants` distinct variant_urls exist for this asset,
            -- new ones are dropped (updates to already-known variants
            -- still apply) -- caps a malicious manifest with thousands of
            -- fabricated variant lines.
            local aid = ARGV[1]
            local variants_json = ARGV[2]
            local max_variants = tonumber(ARGV[3])
            local ns = ARGV[4]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local key = ns .. ':asset:' .. aid .. ':variants'
            local variants = cjson.decode(variants_json)
            local added = 0
            for i = 1, #variants do
                local v = variants[i]
                local already_known = redis.call('HEXISTS', key, v.variant_url) == 1
                local count = redis.call('HLEN', key)
                if already_known or count < max_variants then
                    v.discovered_at = now
                    redis.call('HSET', key, v.variant_url, cjson.encode(v))
                    added = added + 1
                end
            end
            return added
            """
        )

        self._claim_next_script = self.redis_conn.register_script(
            """
            -- Atomic global-queue pop + claim-record write + inflight
            -- insert, matching core/redis_frontier.py's claim shape.
            -- Exactly one caller can ever receive a given aid: ZREM makes
            -- the pop-and-remove indivisible within this single script.
            local lease_ttl = tonumber(ARGV[1])
            local token = ARGV[2]
            local worker_id = ARGV[3]
            local ns = ARGV[4]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local queue_key = ns .. ':jobs:queue'
            local head = redis.call('ZRANGE', queue_key, 0, 0)
            if #head == 0 then
                return false
            end
            local aid = head[1]
            redis.call('ZREM', queue_key, aid)

            local job_key = ns .. ':job:' .. aid
            redis.call('HSET', job_key, 'status', 'claimed', 'claimed_by', worker_id, 'updated_at', tostring(now))

            local claim_key = ns .. ':jobs:claim:' .. aid
            redis.call('HSET', claim_key, 'token', token, 'worker_id', worker_id, 'claimed_at', tostring(now))
            redis.call('ZADD', ns .. ':jobs:inflight', now + lease_ttl, aid)

            local asset_key = ns .. ':asset:' .. aid
            local canonical_url = redis.call('HGET', asset_key, 'canonical_url') or ''
            local media_type = redis.call('HGET', asset_key, 'media_type') or ''
            local priority = redis.call('HGET', job_key, 'priority') or '0'
            local retry_count = redis.call('HGET', job_key, 'retry_count') or '0'
            local target_id = redis.call('HGET', job_key, 'target_id') or ''
            local target_version = redis.call('HGET', job_key, 'target_version') or ''

            return {aid, canonical_url, media_type, priority, retry_count, tostring(now + lease_ttl),
                    target_id, target_version}
            """
        )

        self._renew_lease_script = self.redis_conn.register_script(
            """
            -- Token CAS + inflight score update, atomic. Mirrors
            -- core/redis_frontier.py's renew_claim script exactly.
            local aid = ARGV[1]
            local token = ARGV[2]
            local lease_ttl = tonumber(ARGV[3])
            local ns = ARGV[4]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local claim_key = ns .. ':jobs:claim:' .. aid
            local current_token = redis.call('HGET', claim_key, 'token')
            if (not current_token) or current_token ~= token then
                return false
            end

            local new_expiry = now + lease_ttl
            redis.call('ZADD', ns .. ':jobs:inflight', new_expiry, aid)
            return tostring(new_expiry)
            """
        )

        self._complete_script = self.redis_conn.register_script(
            """
            -- Token CAS, then: clear claim/inflight, mark the job
            -- completed, write durable result fields, and -- iff the
            -- decision is 'confirmed' -- emit the confirmed_match event
            -- (trimmed to a bounded length). All in one round trip so a
            -- stale (reclaimed) worker's completion can never partially
            -- apply: either the whole transition happens, or (CAS
            -- mismatch) none of it does.
            local aid = ARGV[1]
            local token = ARGV[2]
            local result_json = ARGV[3]
            local confirmed_maxlen = ARGV[4]
            local ns = ARGV[5]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local claim_key = ns .. ':jobs:claim:' .. aid
            local current_token = redis.call('HGET', claim_key, 'token')
            if (not current_token) or current_token ~= token then
                return 'stale'
            end

            redis.call('ZREM', ns .. ':jobs:inflight', aid)
            redis.call('DEL', claim_key)

            local job_key = ns .. ':job:' .. aid
            redis.call('HSET', job_key, 'status', 'completed', 'updated_at', tostring(now))
            redis.call('INCR', ns .. ':jobs:completed_total')

            local result = cjson.decode(result_json)
            local args = {}
            for k, v in pairs(result) do
                table.insert(args, k)
                table.insert(args, tostring(v))
            end
            table.insert(args, 'processed_at')
            table.insert(args, tostring(now))
            redis.call('HSET', ns .. ':result:' .. aid, unpack(args))

            if result.aggregate_decision == 'confirmed' then
                local source_domain = redis.call('HGET', ns .. ':asset:' .. aid, 'source_domain') or ''
                local stream_key = ns .. ':events:confirmed_match'
                redis.call('XADD', stream_key, '*',
                    'asset_id', aid,
                    'source_domain', source_domain,
                    'matched_title', result.matched_title or '',
                    'confidence', tostring(result.confidence or ''),
                    'processed_at', tostring(now))
                redis.call('XTRIM', stream_key, 'MAXLEN', '~', confirmed_maxlen)
            end

            return 'ok'
            """
        )

        self._fail_script = self.redis_conn.register_script(
            """
            -- Token CAS, then apply the generic retry/backoff/permanent-
            -- failure state machine (§8). `retryable` is the caller's
            -- classification -- this script never infers it.
            local aid = ARGV[1]
            local token = ARGV[2]
            local error_class = ARGV[3]
            local last_error = ARGV[4]
            local retryable = ARGV[5] == '1'
            local max_retries = tonumber(ARGV[6])
            local base_backoff = tonumber(ARGV[7])
            local max_backoff = tonumber(ARGV[8])
            local ns = ARGV[9]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local claim_key = ns .. ':jobs:claim:' .. aid
            local current_token = redis.call('HGET', claim_key, 'token')
            if (not current_token) or current_token ~= token then
                return 'stale'
            end

            redis.call('ZREM', ns .. ':jobs:inflight', aid)
            redis.call('DEL', claim_key)

            local job_key = ns .. ':job:' .. aid
            local retry_count = tonumber(redis.call('HGET', job_key, 'retry_count')) + 1

            if retryable and retry_count < max_retries then
                local backoff = base_backoff * math.pow(2, retry_count - 1)
                if backoff > max_backoff then backoff = max_backoff end
                redis.call('HSET', job_key,
                    'status', 'retry_scheduled', 'retry_count', tostring(retry_count),
                    'error_class', error_class, 'last_error', last_error, 'updated_at', tostring(now))
                redis.call('ZADD', ns .. ':jobs:retry_scheduled', now + backoff, aid)
                return 'retry_scheduled'
            end

            redis.call('HSET', job_key,
                'status', 'permanent_failure', 'retry_count', tostring(retry_count),
                'error_class', error_class, 'last_error', last_error, 'updated_at', tostring(now))
            redis.call('SADD', ns .. ':jobs:permanent_failure', aid)
            return 'permanent_failure'
            """
        )

        self._mark_forwarded_script = self.redis_conn.register_script(
            """
            -- Phase 4 (docs/architecture/phase-4-crawler-fingerprinter-
            -- bridge.md): token CAS, then transition straight to the
            -- `forwarded` terminal state -- no result hash, no
            -- confirmed_match event (there is no verdict yet, only a
            -- successful hand-off). Mirrors _complete_script's/_fail_
            -- script's CAS shape exactly.
            local aid = ARGV[1]
            local token = ARGV[2]
            local fingerprint_job_id = ARGV[3]
            local ns = ARGV[4]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local claim_key = ns .. ':jobs:claim:' .. aid
            local current_token = redis.call('HGET', claim_key, 'token')
            if (not current_token) or current_token ~= token then
                return 'stale'
            end

            redis.call('ZREM', ns .. ':jobs:inflight', aid)
            redis.call('DEL', claim_key)

            local job_key = ns .. ':job:' .. aid
            redis.call('HSET', job_key,
                'status', 'forwarded',
                'fingerprint_job_id', fingerprint_job_id,
                'updated_at', tostring(now))
            redis.call('INCR', ns .. ':jobs:forwarded_total')

            return 'ok'
            """
        )

        self._reclaim_script = self.redis_conn.register_script(
            """
            -- Two bounded-batch phases in one round trip, mirroring
            -- core/redis_frontier.py's reclaim_and_promote: (a) leases
            -- that expired with no renew/complete -- treated as a
            -- recoverable worker crash (§8: always retryable, since no
            -- explicit failure was ever reported); (b) retry_scheduled
            -- jobs whose backoff has elapsed, promoted back to the queue.
            -- Safe to run redundantly from every worker -- idempotent
            -- because each phase only acts on members it successfully
            -- ZREMs from the source structure first.
            local batch_size = tonumber(ARGV[1])
            local max_retries = tonumber(ARGV[2])
            local base_backoff = tonumber(ARGV[3])
            local max_backoff = tonumber(ARGV[4])
            local ns = ARGV[5]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local inflight_key = ns .. ':jobs:inflight'
            local retry_key = ns .. ':jobs:retry_scheduled'
            local queue_key = ns .. ':jobs:queue'

            local reclaimed = 0
            local requeued = 0

            local expired = redis.call('ZRANGEBYSCORE', inflight_key, '-inf', now, 'LIMIT', 0, batch_size)
            for i = 1, #expired do
                local aid = expired[i]
                redis.call('ZREM', inflight_key, aid)
                redis.call('DEL', ns .. ':jobs:claim:' .. aid)

                local job_key = ns .. ':job:' .. aid
                local current_retry = redis.call('HGET', job_key, 'retry_count')
                if current_retry then
                    local retry_count = tonumber(current_retry) + 1
                    if retry_count < max_retries then
                        local backoff = base_backoff * math.pow(2, retry_count - 1)
                        if backoff > max_backoff then backoff = max_backoff end
                        redis.call('HSET', job_key, 'status', 'retry_scheduled',
                            'retry_count', tostring(retry_count), 'updated_at', tostring(now))
                        redis.call('ZADD', retry_key, now + backoff, aid)
                    else
                        redis.call('HSET', job_key, 'status', 'permanent_failure',
                            'retry_count', tostring(retry_count), 'updated_at', tostring(now))
                        redis.call('SADD', ns .. ':jobs:permanent_failure', aid)
                    end
                    reclaimed = reclaimed + 1
                end
            end

            local due = redis.call('ZRANGEBYSCORE', retry_key, '-inf', now, 'LIMIT', 0, batch_size)
            for i = 1, #due do
                local aid = due[i]
                redis.call('ZREM', retry_key, aid)

                local job_key = ns .. ':job:' .. aid
                local priority = redis.call('HGET', job_key, 'priority')
                if priority then
                    redis.call('HSET', job_key, 'status', 'queued', 'updated_at', tostring(now))
                    local seq = redis.call('INCR', ns .. ':jobs:seq')
                    local score = tonumber(priority) * %(scale)d + seq
                    redis.call('ZADD', queue_key, score, aid)
                    requeued = requeued + 1
                end
            end

            return {reclaimed, requeued}
            """
            % {"scale": _SEQ_SCORE_SCALE}
        )

    # ------------------------------------------------------------------
    # MediaEvidenceStore protocol
    # ------------------------------------------------------------------

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
        """Record one discovery event. `discovery_id` is computed locally
        (sha256 of the normalized URL, §3) -- no round trip is needed to
        determine the asset's identity before mutating it.

        Round trips: 1 (single Lua script -- asset upsert, observation
        append+cap, conditional job creation/priority ratchet). Complexity:
        O(1).
        """
        cleaned = URLUtils.clean_media_url(url)
        if not cleaned:
            raise InvalidMediaURLError(f"Invalid media URL: {url}")
        validate_media_url_length(cleaned)

        normalized_media_type = media_type or URLUtils.classify_media_url(cleaned, mime_type)
        mime_type = truncate_metadata(mime_type) or ""
        source_domain = URLUtils.extract_domain(source_page or cleaned) or ""
        aid = compute_discovery_id(cleaned)

        try:
            self._record_media_link_script(
                args=[
                    aid,
                    cleaned,
                    normalized_media_type,
                    mime_type,
                    source_domain,
                    source_page or "",
                    referrer_url or "",
                    discovered_by,
                    discovery_method,
                    "" if content_length is None else str(content_length),
                    int(priority),
                    self.max_observations_per_asset,
                    self.namespace,
                    self.target_scope.target_id if self.target_scope else "",
                    self.target_scope.target_version if self.target_scope else "",
                ]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error recording media link: {e}")
            raise MediaEvidenceUnavailable(f"record_media_link({cleaned!r}): {e}") from e

        return aid

    def record_manifest_variants(self, asset_id: str, variants: list[dict]) -> None:
        """Round trips: 1 (single Lua script, regardless of variant count).
        Complexity: O(variants)."""
        cleaned_variants = []
        for variant in variants:
            variant_url = URLUtils.clean_media_url(variant.get("url", ""))
            if not variant_url:
                continue
            cleaned_variants.append(
                {
                    "variant_url": variant_url,
                    "bandwidth": variant.get("bandwidth"),
                    "resolution": variant.get("resolution"),
                    "codecs": variant.get("codecs"),
                }
            )
        if not cleaned_variants:
            return

        try:
            self._record_variants_script(
                args=[asset_id, json.dumps(cleaned_variants), self.max_variants_per_asset, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error recording manifest variants: {e}")
            raise MediaEvidenceUnavailable(f"record_manifest_variants({asset_id!r}): {e}") from e

    def _build_asset_dict(self, aid: str, asset_hash: dict, job_hash: dict, result_hash: dict) -> dict:
        job_status = job_hash.get("status", JOB_QUEUED) if job_hash else JOB_QUEUED
        aggregate_decision = result_hash.get("aggregate_decision") if result_hash else None
        return {
            "id": aid,
            "url": asset_hash.get("canonical_url", ""),
            "media_type": asset_hash.get("media_type", ""),
            "source_domain": asset_hash.get("source_domain") or None,
            "mime_type": asset_hash.get("mime_type") or None,
            "content_id": asset_hash.get("content_id") or None,
            "first_seen": _to_optional_float(asset_hash.get("first_seen")),
            "last_seen": _to_optional_float(asset_hash.get("last_seen")),
            "last_source_page": asset_hash.get("last_source_page") or None,
            "last_referrer_url": asset_hash.get("last_referrer_url") or None,
            "last_discovered_by": asset_hash.get("last_discovered_by") or None,
            "last_discovery_method": asset_hash.get("last_discovery_method") or None,
            "observation_count": int(asset_hash.get("observation_count", 0)),
            "status": derive_asset_status(job_status, aggregate_decision),
            "match_confidence": _to_optional_float(result_hash.get("confidence")) if result_hash else None,
            "matched_title": (result_hash.get("matched_title") or None) if result_hash else None,
        }

    def list_media_assets(self, *, limit: Optional[int] = None) -> list[dict]:
        """Round trips: 2 (ZREVRANGE on the last-seen index, then one
        pipelined batch of asset/job/result hash reads). Complexity:
        O(returned assets)."""
        end = limit - 1 if limit is not None else -1
        try:
            aids = self.redis_conn.zrevrange(self._key("assets", "all"), 0, end)
            if not aids:
                return []
            pipe = self.redis_conn.pipeline(transaction=True)
            for aid in aids:
                pipe.hgetall(self._key("asset", aid))
                pipe.hgetall(self._key("job", aid))
                pipe.hgetall(self._key("result", aid))
            raw = pipe.execute()
        except redis.RedisError as e:
            logger.error(f"Redis error listing media assets: {e}")
            raise MediaEvidenceUnavailable(f"list_media_assets: {e}") from e

        assets = []
        for i, aid in enumerate(aids):
            asset_hash, job_hash, result_hash = raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]
            if not asset_hash:
                continue
            assets.append(self._build_asset_dict(aid, asset_hash, job_hash, result_hash))
        return assets

    def list_observations(self, asset_id: str) -> list[dict]:
        """Contract differs from the SQLite backend by design (§17): this
        returns only the most recent `max_observations_per_asset` entries,
        not full history -- `observation_count` on the asset dict (from
        `list_media_assets`) is the exact, never-lossy count.

        Round trips: 1 (LRANGE). Complexity: O(max_observations_per_asset).
        """
        try:
            raw_entries = self.redis_conn.lrange(self._key("asset", asset_id, "observations"), 0, -1)
        except redis.RedisError as e:
            logger.error(f"Redis error listing observations: {e}")
            raise MediaEvidenceUnavailable(f"list_observations({asset_id!r}): {e}") from e

        observations = []
        for raw in raw_entries:
            entry = json.loads(raw)
            content_length = entry.get("content_length")
            observations.append(
                {
                    "asset_id": asset_id,
                    "source_page": entry.get("source_page") or None,
                    "referrer_url": entry.get("referrer_url") or None,
                    "discovered_by": entry.get("discovered_by") or None,
                    "discovery_method": entry.get("discovery_method") or None,
                    "mime_type": entry.get("mime_type") or None,
                    "content_length": int(content_length) if content_length not in (None, "") else None,
                    "observed_at": entry.get("observed_at"),
                }
            )
        return observations

    def list_manifest_variants(self, asset_id: str) -> list[dict]:
        """Round trips: 1 (HGETALL). Complexity: O(max_variants_per_asset)."""
        try:
            raw = self.redis_conn.hgetall(self._key("asset", asset_id, "variants"))
        except redis.RedisError as e:
            logger.error(f"Redis error listing manifest variants: {e}")
            raise MediaEvidenceUnavailable(f"list_manifest_variants({asset_id!r}): {e}") from e

        variants = []
        for variant_url, blob in raw.items():
            entry = json.loads(blob)
            variants.append(
                {
                    "asset_id": asset_id,
                    "variant_url": variant_url,
                    "bandwidth": entry.get("bandwidth"),
                    "resolution": entry.get("resolution"),
                    "codecs": entry.get("codecs"),
                    "discovered_at": entry.get("discovered_at"),
                }
            )
        variants.sort(key=lambda v: (v["bandwidth"] if v["bandwidth"] is not None else -1, v["variant_url"]))
        return variants

    def _build_job_dict(self, aid: str, job_hash: dict) -> dict:
        return {
            "asset_id": aid,
            "status": job_hash.get("status"),
            "priority": int(job_hash.get("priority", 0)),
            "retry_count": int(job_hash.get("retry_count", 0)),
            "error_class": job_hash.get("error_class") or None,
            "last_error": job_hash.get("last_error") or None,
            "claimed_by": job_hash.get("claimed_by") or None,
            "created_at": _to_optional_float(job_hash.get("created_at")),
            "updated_at": _to_optional_float(job_hash.get("updated_at")),
            "target_id": job_hash.get("target_id") or None,
            "target_version": job_hash.get("target_version") or None,
            "fingerprint_job_id": job_hash.get("fingerprint_job_id") or None,
        }

    def get_fingerprint_jobs(self, statuses: Optional[Sequence[str]] = None) -> list[dict]:
        """Introspection/CLI/testing surface, not a hot-path operation.
        `completed` jobs are not returned -- no bounded index exists for
        them by design (§12); read `list_media_assets`/results for
        completed evidence instead.

        Round trips: up to len(statuses) index reads + 1 pipelined batch of
        job hash reads. Complexity: O(returned jobs).
        """
        wanted = list(statuses) if statuses else list(_STATUS_INDEX.keys())
        try:
            aids: set[str] = set()
            for status in wanted:
                index = _STATUS_INDEX.get(status)
                if index is None:
                    continue
                kind, suffix = index
                key = self._key(*suffix)
                if kind == "zset":
                    aids.update(self.redis_conn.zrange(key, 0, -1))
                else:
                    aids.update(self.redis_conn.smembers(key))

            if not aids:
                return []

            ordered_aids = list(aids)
            pipe = self.redis_conn.pipeline(transaction=True)
            for aid in ordered_aids:
                pipe.hgetall(self._key("job", aid))
            raw = pipe.execute()
        except redis.RedisError as e:
            logger.error(f"Redis error listing fingerprint jobs: {e}")
            raise MediaEvidenceUnavailable(f"get_fingerprint_jobs: {e}") from e

        jobs = []
        for aid, job_hash in zip(ordered_aids, raw):
            if not job_hash:
                continue
            jobs.append(self._build_job_dict(aid, job_hash))
        jobs.sort(key=lambda j: (j["priority"], j["updated_at"] or 0.0))
        return jobs

    def claim_next_fingerprint_job(self, worker_id: str) -> Optional[FingerprintJob]:
        """Round trips: 1 (single Lua script -- ZRANGE+ZREM the queue head,
        write the claim/inflight records, read back canonical_url/media_type
        for the caller). Complexity: O(1)."""
        token = uuid.uuid4().hex
        try:
            result = self._claim_next_script(
                args=[self.fingerprint_lease_ttl, token, worker_id, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error claiming fingerprint job: {e}")
            raise MediaEvidenceUnavailable(f"claim_next_fingerprint_job: {e}") from e

        if not result:
            return None

        aid, canonical_url, media_type, priority, retry_count, lease_expires_at, target_id, target_version = result
        return FingerprintJob(
            asset_id=aid,
            token=token,
            canonical_url=canonical_url,
            media_type=media_type,
            priority=int(priority),
            retry_count=int(retry_count),
            lease_expires_at=float(lease_expires_at),
            target_id=target_id or None,
            target_version=target_version or None,
        )

    def renew_job_lease(self, asset_id: str, token: str) -> bool:
        """Round trips: 1 (HGET + conditional ZADD in one Lua script).
        Complexity: O(1)."""
        try:
            result = self._renew_lease_script(
                args=[asset_id, token, self.fingerprint_lease_ttl, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error renewing job lease: {e}")
            raise MediaEvidenceUnavailable(f"renew_job_lease({asset_id!r}): {e}") from e

        return bool(result)

    def _encode_result_fields(self, result: FingerprintResult) -> str:
        fields: dict[str, object] = {
            "aggregate_decision": result.aggregate_decision,
            "worker_id": result.worker_id,
        }
        if result.confidence is not None:
            fields["confidence"] = result.confidence
        if result.dinov2_similarity is not None:
            fields["dinov2_similarity"] = result.dinov2_similarity
        if result.phash_score is not None:
            fields["phash_score"] = result.phash_score
        if result.audio_score is not None:
            fields["audio_score"] = result.audio_score
        if result.temporal_verified is not None:
            fields["temporal_verified"] = result.temporal_verified
        if result.algorithm_versions:
            fields["algorithm_versions"] = json.dumps(result.algorithm_versions)
        matched_title = truncate_metadata(result.matched_title)
        if matched_title:
            fields["matched_title"] = matched_title
        return json.dumps(fields)

    def complete_fingerprint_job(self, asset_id: str, token: str, *, result: FingerprintResult) -> bool:
        """Round trips: 1 (single Lua script -- token CAS, clear claim/
        inflight, write the result hash, conditionally emit+trim the
        confirmed_match stream). Complexity: O(1). Returns `False` for a
        stale/unknown claim -- the caller must not treat this as success
        (docs/architecture/media-evidence-redis-design.md §20 scenario 4)."""
        payload = self._encode_result_fields(result)
        try:
            outcome = self._complete_script(
                args=[asset_id, token, payload, self.confirmed_match_stream_maxlen, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error completing fingerprint job: {e}")
            raise MediaEvidenceUnavailable(f"complete_fingerprint_job({asset_id!r}): {e}") from e

        if outcome == "stale":
            logger.debug(f"Stale fingerprint job completion ignored: {asset_id}")
            return False
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
        """Round trips: 1 (single Lua script -- token CAS, then the
        generic retry/backoff/permanent-failure transition). Complexity:
        O(1). Returns `False` for a stale/unknown claim."""
        bounded_error_class = truncate_metadata(error_class) or ""
        bounded_last_error = truncate_metadata(last_error) or ""
        try:
            outcome = self._fail_script(
                args=[
                    asset_id,
                    token,
                    bounded_error_class,
                    bounded_last_error,
                    "1" if retryable else "0",
                    self.max_retries,
                    self.base_backoff,
                    self.max_backoff,
                    self.namespace,
                ]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error failing fingerprint job: {e}")
            raise MediaEvidenceUnavailable(f"fail_fingerprint_job({asset_id!r}): {e}") from e

        if outcome == "stale":
            logger.debug(f"Stale fingerprint job failure ignored: {asset_id}")
            return False
        return True

    def mark_fingerprint_job_forwarded(self, asset_id: str, token: str, *, fingerprint_job_id: str) -> bool:
        """Round trips: 1 (single Lua script -- token CAS, clear claim/
        inflight, transition to the `forwarded` terminal state). Complexity:
        O(1). See `JOB_FORWARDED`'s docstring (storage/media_evidence_store.py)
        for why this is distinct from `complete_fingerprint_job`. Returns
        `False` for a stale/unknown claim -- the caller must not treat this
        as success (the job may already be owned by a different claimer)."""
        try:
            outcome = self._mark_forwarded_script(args=[asset_id, token, fingerprint_job_id, self.namespace])
        except redis.RedisError as e:
            logger.error(f"Redis error marking fingerprint job forwarded: {e}")
            raise MediaEvidenceUnavailable(f"mark_fingerprint_job_forwarded({asset_id!r}): {e}") from e

        if outcome == "stale":
            logger.debug(f"Stale fingerprint job forward-ack ignored: {asset_id}")
            return False
        return True

    def reclaim_expired_jobs(self, batch_size: int = 200) -> tuple[int, int]:
        """Not run automatically by this class -- a caller (mirroring
        `CrawlerManager._recovery_loop`'s use of `reclaim_and_promote`)
        must schedule this periodically. Safe to call redundantly/
        concurrently from every worker.

        Round trips: 1 (single Lua script covering both phases).
        Complexity: O(batch_size), independent of total job count.
        """
        batch = batch_size if batch_size is not None else self.reclaim_batch_size
        try:
            result = self._reclaim_script(
                args=[batch, self.max_retries, self.base_backoff, self.max_backoff, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error reclaiming expired fingerprint jobs: {e}")
            raise MediaEvidenceUnavailable(f"reclaim_expired_jobs: {e}") from e

        reclaimed, requeued = result
        return int(reclaimed), int(requeued)

    def get_status_counts(self) -> dict[str, int]:
        """Round trips: 1 (pipelined ZCARD/ZCARD/ZCARD/SCARD/GET).
        Complexity: O(1)."""
        try:
            pipe = self.redis_conn.pipeline(transaction=True)
            pipe.zcard(self._key("jobs", "queue"))
            pipe.zcard(self._key("jobs", "inflight"))
            pipe.zcard(self._key("jobs", "retry_scheduled"))
            pipe.scard(self._key("jobs", "permanent_failure"))
            pipe.get(self._key("jobs", "completed_total"))
            pipe.get(self._key("jobs", "forwarded_total"))
            queued, claimed, retry_scheduled, permanent_failure, completed_total, forwarded_total = pipe.execute()
        except redis.RedisError as e:
            logger.error(f"Redis error getting status counts: {e}")
            raise MediaEvidenceUnavailable(f"get_status_counts: {e}") from e

        return {
            JOB_QUEUED: queued,
            JOB_CLAIMED: claimed,
            JOB_RETRY_SCHEDULED: retry_scheduled,
            JOB_PERMANENT_FAILURE: permanent_failure,
            JOB_COMPLETED: int(completed_total) if completed_total else 0,
            JOB_FORWARDED: int(forwarded_total) if forwarded_total else 0,
        }

    def read_confirmed_match_events(self, count: int = 100) -> list[dict]:
        """Redis-only test/ops helper, not part of `MediaEvidenceStore` --
        no consumer exists yet (§19 explicitly defers the domain-scoring
        consumer to a future phase). Exists so tests can assert the event
        side of `complete_fingerprint_job` without a consumer to read it.
        """
        try:
            entries = self.redis_conn.xrange(self._key("events", "confirmed_match"), count=count)
        except redis.RedisError as e:
            logger.error(f"Redis error reading confirmed_match events: {e}")
            raise MediaEvidenceUnavailable(f"read_confirmed_match_events: {e}") from e

        return [dict(fields) for _stream_id, fields in entries]

    def clear(self) -> None:
        """Testing/reset only -- uses SCAN, never on a hot path."""
        try:
            pattern = f"{self.namespace}:*"
            cursor = 0
            while True:
                cursor, keys = self.redis_conn.scan(cursor, match=pattern, count=200)
                if keys:
                    self.redis_conn.delete(*keys)
                if cursor == 0:
                    break
            logger.info("Cleared media evidence state")
        except redis.RedisError as e:
            logger.error(f"Redis error clearing media evidence: {e}")
            raise MediaEvidenceUnavailable(f"clear: {e}") from e

    def close(self) -> None:
        try:
            self.redis_conn.close()
        except redis.RedisError as e:
            logger.error(f"Redis error on close: {e}")
