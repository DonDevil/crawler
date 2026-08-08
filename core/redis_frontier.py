"""Redis-backed URL frontier for distributed crawling across multiple workers.

Implements the `Frontier` protocol (core/frontier.py) against the Redis v2
keyspace and claim/lease design in docs/architecture/frontier-adr.md §5-§6.
The local `URLFrontier` (core/url_frontier.py) is the behavioral reference;
this backend satisfies the same external contract, but its internals are
built for coordination across many concurrent workers instead of a single
in-process heap.

Every state-changing operation below is a single Lua script (one Redis round
trip, no unnecessary chatter), and no operation scans the domain keyspace to
schedule work -- `domain_heads` (a ZSET mirroring each domain queue's current
head) plays the role the local frontier's `heapq` plays, so picking the
globally next-best domain is an O(K)-bounded ZRANGE, not an O(domains) scan.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Optional
from urllib.parse import urlparse

import redis
from loguru import logger

from core.frontier import FrontierClaim
from storage.url_database import URLDatabase
from utils.url_utils import URLUtils

# Score formula shared by domain queues and domain_heads: priority is the
# major sort key, the monotonic sequence number breaks ties in insertion
# order -- the same (priority, sequence) ordering the local frontier's heap
# uses. Kept as a module constant since both Lua scripts and Python need it.
_SEQ_SCORE_SCALE = 1_000_000

# Upper bound on how many times get_next_url() will retry claim_next after
# skipping a blacklisted-while-queued URL, before giving up and returning
# None. Purely a safety valve against a pathological run of consecutive
# blacklisted URLs; never hit in practice.
_MAX_BLACKLIST_SKIPS_PER_CALL = 200


class RedisURLFrontier:
    """Distributed URL frontier using Redis for multi-worker coordination.

    Provides, across N workers sharing one Redis instance:
    - Global dedup (`urls:known`, monotonic -- see core/frontier.py's `add_url`
      guarantee)
    - Atomic claim ownership via `FrontierClaim.token`, validated by every
      completion/renewal call so a stale (since-reclaimed) worker can never
      complete a newer claim on the same URL
    - Global cross-domain priority scheduling through `domain_heads`
    - Per-domain rate limiting that coexists with global priority (a
      rate-gated domain is skipped, not blocking, so a lower-priority
      eligible domain still yields work)
    - Retry/backoff state (`retry_scheduled`) and accurate status counts

    Background lease recovery (the asyncio task that periodically calls
    `reclaim_and_promote`) and claim-renewal wiring (`run_with_heartbeat` in
    the crawler backends) are intentionally NOT part of this class's callers
    yet -- see docs/architecture/frontier-step3.md "known limitations".
    `reclaim_and_promote` itself is implemented and safe to call directly
    (e.g. from tests or a future scheduled task).
    """

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        rate_limit: float = 1.0,
        url_database: URLDatabase | None = None,
        namespace: str = "crawler",
        max_retries: int = 3,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        lease_ttl: float = 90.0,
        domain_scan_limit: int = 50,
        reclaim_batch_size: int = 200,
        terminal_meta_ttl_seconds: Optional[float] = None,
    ):
        """Initialize Redis frontier.

        Args:
            redis_host: Redis server hostname
            redis_port: Redis server port
            redis_db: Redis database number (0-15)
            rate_limit: Minimum seconds between requests to same domain
            url_database: Optional SQLite database for fallback persistence
            namespace: Redis key namespace prefix
            max_retries: Frontier-level retry attempts before failed_permanent
            base_backoff: Base seconds for exponential retry backoff
            max_backoff: Cap on retry backoff seconds
            lease_ttl: Seconds an inflight claim is valid before it becomes
                eligible for lease-expiry reclaim (reclaim itself is not
                run automatically by this class -- see class docstring)
            domain_scan_limit: Max candidate domains `get_next_url` examines
                from `domain_heads` per call (the `K` bound from ADR §5/§6;
                caps worst-case Lua execution time when many domains are
                simultaneously rate-limited)
            reclaim_batch_size: Default batch size for `reclaim_and_promote`
            terminal_meta_ttl_seconds: TTL applied to `meta:{url}` when a URL
                reaches a terminal state; `None`/`0` deletes it immediately
                (ADR §9's proposed default -- retention policy is a later
                product decision, not a Step 3 correctness concern)
        """
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self.rate_limit = rate_limit
        self.url_database = url_database
        self.namespace = namespace

        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.lease_ttl = lease_ttl
        self.domain_scan_limit = domain_scan_limit
        self.reclaim_batch_size = reclaim_batch_size
        self.terminal_meta_ttl_seconds = terminal_meta_ttl_seconds or 0

        try:
            self.redis_conn = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                socket_keepalive=True,
                health_check_interval=30,
                decode_responses=True,
            )
            # Test connection
            self.redis_conn.ping()
            logger.info(f"Connected to Redis at {redis_host}:{redis_port}/{redis_db}")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

        self._init_lua_scripts()

    # ------------------------------------------------------------------
    # Lua scripts -- every state-changing operation is exactly one of
    # these, so it is exactly one Redis round trip regardless of how many
    # domains or workers exist. See each method's docstring below for the
    # round-trip/complexity accounting.
    # ------------------------------------------------------------------

    def _init_lua_scripts(self) -> None:
        """Register the atomic Lua operations (ADR §5)."""

        self._add_url_script = self.redis_conn.register_script(
            """
            local url = ARGV[1]
            local domain = ARGV[2]
            local priority = tonumber(ARGV[3])
            local source_query = ARGV[4]
            local ns = ARGV[5]

            local time_result = redis.call('TIME')
            local now = time_result[1] .. '.' .. time_result[2]

            local known_key = ns .. ':urls:known'
            if redis.call('SISMEMBER', known_key, url) == 1 then
                return 0
            end

            redis.call('SADD', known_key, url)

            local seq = redis.call('INCR', ns .. ':seq')
            local score = priority * %(scale)d + seq

            local queue_key = ns .. ':domain:' .. domain .. ':queue'
            redis.call('ZADD', queue_key, score, url)

            local head = redis.call('ZRANGE', queue_key, 0, 0, 'WITHSCORES')
            redis.call('ZADD', ns .. ':domain_heads', tonumber(head[2]), domain)
            redis.call('SADD', ns .. ':domains:active', domain)

            redis.call('HSET', ns .. ':meta:' .. url,
                'source_query', source_query,
                'domain', domain,
                'priority', priority,
                'first_seen', now)

            return 1
            """
            % {"scale": _SEQ_SCORE_SCALE}
        )

        self._claim_next_script = self.redis_conn.register_script(
            """
            local k = tonumber(ARGV[1])
            local lease_ttl = tonumber(ARGV[2])
            local rate_limit = tonumber(ARGV[3])
            local token = ARGV[4]
            local ns = ARGV[5]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local domain_heads_key = ns .. ':domain_heads'
            local domains_active_key = ns .. ':domains:active'

            local candidates = redis.call('ZRANGE', domain_heads_key, 0, k - 1)

            for i = 1, #candidates do
                local domain = candidates[i]
                local queue_key = ns .. ':domain:' .. domain .. ':queue'
                local next_time_key = ns .. ':domain:' .. domain .. ':next_time'

                local head = redis.call('ZRANGE', queue_key, 0, 0, 'WITHSCORES')
                if #head == 0 then
                    -- domain_heads entry is stale (queue already drained by
                    -- another path); self-heal and move on to next candidate.
                    redis.call('ZREM', domain_heads_key, domain)
                    redis.call('SREM', domains_active_key, domain)
                else
                    local next_time = redis.call('GET', next_time_key)
                    if next_time and tonumber(next_time) > now then
                        -- Rate-gated: leave this domain's head in place
                        -- (still eligible later) and try the next candidate,
                        -- so a lower-priority eligible domain isn't blocked.
                    else
                        local url = head[1]
                        local score = tonumber(head[2])
                        local priority = math.floor(score / %(scale)d)

                        redis.call('ZREM', queue_key, url)

                        local new_head = redis.call('ZRANGE', queue_key, 0, 0, 'WITHSCORES')
                        if #new_head == 0 then
                            redis.call('ZREM', domain_heads_key, domain)
                            redis.call('SREM', domains_active_key, domain)
                        else
                            redis.call('ZADD', domain_heads_key, tonumber(new_head[2]), domain)
                        end

                        redis.call('SET', next_time_key, tostring(now + rate_limit))

                        local attempt = redis.call('INCR', ns .. ':attempts:' .. url)
                        redis.call('HSET', ns .. ':claim:' .. url,
                            'token', token,
                            'attempt', attempt,
                            'domain', domain,
                            'priority', priority,
                            'claimed_at', now)
                        redis.call('ZADD', ns .. ':inflight', now + lease_ttl, url)

                        local source_query = redis.call('HGET', ns .. ':meta:' .. url, 'source_query') or ''

                        return {url, tostring(attempt), domain, tostring(priority),
                                token, tostring(now + lease_ttl), source_query}
                    end
                end
            end

            return false
            """
            % {"scale": _SEQ_SCORE_SCALE}
        )

        self._complete_claim_script = self.redis_conn.register_script(
            """
            local url = ARGV[1]
            local token = ARGV[2]
            local outcome = ARGV[3]
            local max_retries = tonumber(ARGV[4])
            local base_backoff = tonumber(ARGV[5])
            local max_backoff = tonumber(ARGV[6])
            local terminal_ttl = tonumber(ARGV[7])
            local ns = ARGV[8]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local claim_key = ns .. ':claim:' .. url
            local current_token = redis.call('HGET', claim_key, 'token')

            if (not current_token) or current_token ~= token then
                return 'stale'
            end

            redis.call('ZREM', ns .. ':inflight', url)

            local meta_key = ns .. ':meta:' .. url
            local attempts_key = ns .. ':attempts:' .. url

            local function finalize_terminal(set_key)
                redis.call('SADD', set_key, url)
                redis.call('DEL', attempts_key)
                if terminal_ttl and terminal_ttl > 0 then
                    redis.call('EXPIRE', meta_key, terminal_ttl)
                else
                    redis.call('DEL', meta_key)
                end
            end

            if outcome == 'visited' then
                redis.call('DEL', claim_key)
                finalize_terminal(ns .. ':urls:visited')
                return 'visited'
            elseif outcome == 'skipped' then
                redis.call('DEL', claim_key)
                finalize_terminal(ns .. ':urls:skipped')
                return 'skipped'
            else
                local attempt = tonumber(redis.call('HGET', claim_key, 'attempt'))
                redis.call('DEL', claim_key)

                if attempt < max_retries then
                    local backoff = base_backoff * math.pow(2, attempt - 1)
                    if backoff > max_backoff then backoff = max_backoff end
                    redis.call('ZADD', ns .. ':retry_scheduled', now + backoff, url)
                    return 'retry_scheduled'
                else
                    finalize_terminal(ns .. ':urls:failed_permanent')
                    return 'failed_permanent'
                end
            end
            """
        )

        self._renew_claim_script = self.redis_conn.register_script(
            """
            local url = ARGV[1]
            local token = ARGV[2]
            local lease_ttl = tonumber(ARGV[3])
            local ns = ARGV[4]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local claim_key = ns .. ':claim:' .. url
            local current_token = redis.call('HGET', claim_key, 'token')
            if (not current_token) or current_token ~= token then
                return false
            end

            local new_expiry = now + lease_ttl
            redis.call('ZADD', ns .. ':inflight', new_expiry, url)
            return tostring(new_expiry)
            """
        )

        self._reclaim_and_promote_script = self.redis_conn.register_script(
            """
            local batch_size = tonumber(ARGV[1])
            local max_retries = tonumber(ARGV[2])
            local base_backoff = tonumber(ARGV[3])
            local max_backoff = tonumber(ARGV[4])
            local ns = ARGV[5]

            local time_result = redis.call('TIME')
            local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)

            local inflight_key = ns .. ':inflight'
            local retry_key = ns .. ':retry_scheduled'
            local domain_heads_key = ns .. ':domain_heads'
            local domains_active_key = ns .. ':domains:active'

            local reclaimed = 0
            local requeued = 0

            -- (a) abandoned inflight claims: lease expired and never
            -- invalidated by a completion call. Same attempt-vs-max_retries
            -- decision as complete_claim's failed branch.
            local expired = redis.call('ZRANGEBYSCORE', inflight_key, '-inf', now, 'LIMIT', 0, batch_size)
            for i = 1, #expired do
                local url = expired[i]
                local claim_key = ns .. ':claim:' .. url
                local attempt = tonumber(redis.call('HGET', claim_key, 'attempt'))

                redis.call('ZREM', inflight_key, url)
                redis.call('DEL', claim_key)

                if attempt then
                    if attempt < max_retries then
                        local backoff = base_backoff * math.pow(2, attempt - 1)
                        if backoff > max_backoff then backoff = max_backoff end
                        redis.call('ZADD', retry_key, now + backoff, url)
                    else
                        redis.call('SADD', ns .. ':urls:failed_permanent', url)
                        redis.call('DEL', ns .. ':attempts:' .. url)
                        redis.call('DEL', ns .. ':meta:' .. url)
                    end
                    reclaimed = reclaimed + 1
                end
            end

            -- (b) retry-scheduled URLs whose backoff has expired: promote
            -- back into their domain queue and resync domain_heads.
            local due = redis.call('ZRANGEBYSCORE', retry_key, '-inf', now, 'LIMIT', 0, batch_size)
            for i = 1, #due do
                local url = due[i]
                redis.call('ZREM', retry_key, url)

                local meta_key = ns .. ':meta:' .. url
                local domain = redis.call('HGET', meta_key, 'domain')
                local priority = tonumber(redis.call('HGET', meta_key, 'priority'))

                if domain and priority then
                    local seq = redis.call('INCR', ns .. ':seq')
                    local score = priority * %(scale)d + seq
                    local queue_key = ns .. ':domain:' .. domain .. ':queue'
                    redis.call('ZADD', queue_key, score, url)

                    local head = redis.call('ZRANGE', queue_key, 0, 0, 'WITHSCORES')
                    redis.call('ZADD', domain_heads_key, tonumber(head[2]), domain)
                    redis.call('SADD', domains_active_key, domain)
                    requeued = requeued + 1
                end
            end

            return {reclaimed, requeued}
            """
            % {"scale": _SEQ_SCORE_SCALE}
        )

    def _key(self, *parts: str) -> str:
        """Build namespaced Redis key (used outside Lua scripts)."""
        return f"{self.namespace}:" + ":".join(parts)

    # ------------------------------------------------------------------
    # Frontier protocol
    # ------------------------------------------------------------------

    def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool:
        """Add a URL to the frontier if not already known.

        Round trips: 1 (single Lua script -- SADD known, INCR seq, ZADD
        domain queue, resync domain_heads/domains:active, HSET meta).
        Complexity: O(1).
        """
        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            if URLUtils.is_blacklisted(url):
                logger.debug(f"Ignoring blacklisted URL before queueing: {url}")
            return False

        domain = urlparse(cleaned).netloc

        try:
            result = self._add_url_script(
                args=[
                    cleaned,
                    domain,
                    int(priority),
                    source_query or "",
                    self.namespace,
                ]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error adding URL: {e}")
            return False

        if result:
            if self.url_database is not None:
                self.url_database.add_url(cleaned, status="queued")
            logger.debug(f"Added to frontier: {cleaned}")
        return bool(result)

    def get_next_url(self) -> FrontierClaim | None:
        """Atomically claim the next crawlable URL.

        Round trips: 1 in the common case (single Lua script --
        `ZRANGE domain_heads 0 K-1`, skip rate-gated domains, pop the first
        eligible domain's queue head, resync domain_heads/domains:active,
        INCR attempts, HSET the claim CAS record, ZADD inflight). If the
        claimed URL is blacklisted (rare -- only possible if it became
        blacklisted after being queued), one extra round trip marks it
        skipped and the script is re-run; this is bounded and self-
        terminating, matching the local frontier's blacklist-while-queued
        eviction behavior.
        Complexity: O(K) server-side Lua work per call, K = domain_scan_limit
        (default 50) -- never O(domains) network round trips, and never a
        SCAN.
        """
        skips = 0
        while skips < _MAX_BLACKLIST_SKIPS_PER_CALL:
            token = uuid.uuid4().hex

            try:
                result = self._claim_next_script(
                    args=[
                        self.domain_scan_limit,
                        self.lease_ttl,
                        self.rate_limit,
                        token,
                        self.namespace,
                    ]
                )
            except redis.RedisError as e:
                logger.error(f"Redis error getting next URL: {e}")
                return None

            if not result:
                return None

            url, attempt, domain, priority, claim_token, lease_expires_at, source_query = result

            claim = FrontierClaim(
                url=url,
                token=claim_token,
                attempt=int(attempt),
                domain=domain,
                priority=int(priority),
                lease_expires_at=float(lease_expires_at),
                source_query=source_query,
            )

            if URLUtils.is_blacklisted(url):
                logger.info(f"Skipping blacklisted URL from frontier: {url}")
                self.mark_skipped(claim)
                skips += 1
                continue

            if self.url_database is not None:
                self.url_database.update_status(url, "pending")

            return claim

        logger.warning(
            f"get_next_url: hit blacklist-skip cap ({_MAX_BLACKLIST_SKIPS_PER_CALL}) in one call"
        )
        return None

    def renew_claim(self, claim: FrontierClaim) -> FrontierClaim | None:
        """Extend a claim's lease iff its token is still the current owner.

        Round trips: 1 (HGET + conditional ZADD in one Lua script).
        Complexity: O(1).
        """
        try:
            result = self._renew_claim_script(
                args=[claim.url, claim.token, self.lease_ttl, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error renewing claim: {e}")
            return None

        if not result:
            return None

        return replace(claim, lease_expires_at=float(result))

    def _complete(self, claim: FrontierClaim, outcome: str, error: str = "") -> str:
        """Shared implementation for mark_visited/mark_failed/mark_skipped.

        Round trips: 1 (single Lua script -- validate token, ZREM inflight,
        DEL claim, then branch to the terminal set or retry_scheduled).
        Complexity: O(1).
        """
        try:
            result = self._complete_claim_script(
                args=[
                    claim.url,
                    claim.token,
                    outcome,
                    self.max_retries,
                    self.base_backoff,
                    self.max_backoff,
                    self.terminal_meta_ttl_seconds,
                    self.namespace,
                ]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error completing claim ({outcome}): {e}")
            return "error"

        if result == "stale":
            logger.debug(f"Stale claim ignored ({outcome}): {claim.url}")
            return result

        if outcome == "failed":
            if result == "retry_scheduled":
                logger.debug(
                    f"Retry scheduled for {claim.url} "
                    f"(attempt {claim.attempt}/{self.max_retries}): {error}"
                )
            else:
                logger.info(
                    f"{claim.url} failed permanently after {claim.attempt} attempts: {error}"
                )

        if self.url_database is not None:
            db_status = {
                "visited": "visited",
                "skipped": "skipped",
                "retry_scheduled": "queued",
                "failed_permanent": "failed",
            }.get(result, outcome)
            self.url_database.update_status(claim.url, db_status)

        return result

    def mark_visited(self, claim: FrontierClaim) -> None:
        """Mark a claimed URL as terminally visited, iff the claim is current."""
        self._complete(claim, "visited")

    def mark_failed(self, claim: FrontierClaim, error: str = "") -> None:
        """Record a failed attempt; frontier decides retry-vs-terminal from
        `claim.attempt` vs. `max_retries`, iff the claim is current."""
        self._complete(claim, "failed", error)

    def mark_skipped(self, claim: FrontierClaim) -> None:
        """Mark a claimed URL as terminally skipped (never retried), iff current."""
        self._complete(claim, "skipped")

    def reclaim_and_promote(self, batch_size: int | None = None) -> tuple[int, int]:
        """Reclaim abandoned inflight claims and promote due retries.

        Not wired to run automatically (no background task calls this yet --
        see docs/architecture/frontier-step3.md). Safe to call directly, e.g.
        from tests or a future scheduled task.

        Round trips: 1 (single Lua script covering both phases).
        Complexity: O(batch_size), bounded and independent of total domain
        or URL count -- never O(domains).

        Returns:
            (reclaimed, requeued) -- counts of abandoned inflight claims
            processed and retry_scheduled entries promoted back to a domain
            queue, respectively.
        """
        batch = batch_size if batch_size is not None else self.reclaim_batch_size
        try:
            result = self._reclaim_and_promote_script(
                args=[batch, self.max_retries, self.base_backoff, self.max_backoff, self.namespace]
            )
        except redis.RedisError as e:
            logger.error(f"Redis error during reclaim_and_promote: {e}")
            return (0, 0)

        reclaimed, requeued = result
        return int(reclaimed), int(requeued)

    def has_pending(self) -> bool:
        """Return True when the frontier still has queued, in-flight, or
        retry-pending work.

        Round trips: 1 (pipelined SCARD/SCARD/SCARD/SCARD).
        Complexity: O(1) -- derived from set/zset cardinalities, never a scan.
        """
        try:
            pipe = self.redis_conn.pipeline(transaction=True)
            pipe.scard(self._key("urls", "known"))
            pipe.scard(self._key("urls", "visited"))
            pipe.scard(self._key("urls", "skipped"))
            pipe.scard(self._key("urls", "failed_permanent"))
            known, visited, skipped, failed_permanent = pipe.execute()
        except redis.RedisError as e:
            logger.error(f"Redis error checking has_pending: {e}")
            return False

        non_terminal = known - visited - skipped - failed_permanent
        return non_terminal > 0

    def pending_count(self) -> int:
        """Return the number of queued + retry-pending URLs (in-flight excluded).

        Round trips: 1 (pipelined cardinalities).
        Complexity: O(1).
        """
        counts = self.get_status_counts()
        return counts["queued"] + counts["retry_scheduled"]

    def get_source_query(self, url: str) -> str:
        """Return the source query for a URL, or empty string if unknown/expired.

        Round trips: 1 (HGET). Complexity: O(1).
        """
        cleaned = URLUtils.normalize_url(url)
        if not cleaned:
            return ""

        try:
            source_query = self.redis_conn.hget(self._key("meta", cleaned), "source_query")
            return source_query or ""
        except redis.RedisError:
            return ""

    def get_status_counts(self) -> dict[str, int]:
        """Return counts for every state in the frontier's URL state machine.

        `queued` is derived as `known - visited - skipped - failed_permanent
        - inflight - retry_scheduled` rather than summed over every domain
        queue, so this stays O(1) regardless of how many domains exist --
        per-domain queue lengths are never enumerated.

        Round trips: 1 (pipelined SCARD x4 + ZCARD x2).
        Complexity: O(1).
        """
        try:
            pipe = self.redis_conn.pipeline(transaction=True)
            pipe.scard(self._key("urls", "known"))
            pipe.scard(self._key("urls", "visited"))
            pipe.scard(self._key("urls", "skipped"))
            pipe.scard(self._key("urls", "failed_permanent"))
            pipe.zcard(self._key("inflight"))
            pipe.zcard(self._key("retry_scheduled"))
            known, visited, skipped, failed_permanent, inflight, retry_scheduled = pipe.execute()
        except redis.RedisError as e:
            logger.error(f"Redis error getting status counts: {e}")
            return {
                "queued": 0,
                "inflight": 0,
                "retry_scheduled": 0,
                "visited": 0,
                "skipped": 0,
                "failed_permanent": 0,
            }

        queued = max(0, known - visited - skipped - failed_permanent - inflight - retry_scheduled)
        return {
            "queued": queued,
            "inflight": inflight,
            "retry_scheduled": retry_scheduled,
            "visited": visited,
            "skipped": skipped,
            "failed_permanent": failed_permanent,
        }

    def clear(self) -> None:
        """Clear all frontier state (testing/reset only -- uses SCAN, which
        is fine here since this is never on the claim/scheduling hot path)."""
        try:
            pattern = f"{self.namespace}:*"
            cursor = 0
            while True:
                cursor, keys = self.redis_conn.scan(cursor, match=pattern, count=200)
                if keys:
                    self.redis_conn.delete(*keys)
                if cursor == 0:
                    break
            logger.info("Cleared frontier state")
        except redis.RedisError as e:
            logger.error(f"Redis error clearing frontier: {e}")

    def close(self) -> None:
        """Close Redis connection."""
        try:
            self.redis_conn.close()
            logger.info("Closed Redis connection")
        except redis.RedisError as e:
            logger.error(f"Redis error on close: {e}")
