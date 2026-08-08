"""Redis-backed URL frontier for distributed crawling across multiple workers."""

from __future__ import annotations

import time
from typing import Iterable, Optional
from urllib.parse import urlparse

import redis
from loguru import logger

from storage.url_database import URLDatabase
from utils.url_utils import URLUtils


class RedisURLFrontier:
    """Distributed URL frontier using Redis for multi-worker coordination.
    
    Supports 4+ workers sharing a common URL queue with:
    - Global deduplication
    - Per-domain rate limiting
    - Priority-based scheduling
    - Atomic operations via Lua scripts
    """

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        rate_limit: float = 1.0,
        url_database: URLDatabase | None = None,
        namespace: str = "crawler",
    ):
        """Initialize Redis frontier.

        Args:
            redis_host: Redis server hostname
            redis_port: Redis server port
            redis_db: Redis database number (0-15)
            rate_limit: Minimum seconds between requests to same domain
            url_database: Optional SQLite database for fallback persistence
            namespace: Redis key namespace prefix
        """
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self.rate_limit = rate_limit
        self.url_database = url_database
        self.namespace = namespace

        try:
            self.redis_conn = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                socket_keepalive=True,
                health_check_interval=30,
            )
            # Test connection
            self.redis_conn.ping()
            logger.info(f"Connected to Redis at {redis_host}:{redis_port}/{redis_db}")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

        self._init_lua_scripts()

    def _init_lua_scripts(self) -> None:
        """Initialize Lua scripts for atomic operations."""
        # Lua script for atomic add_url operation
        self._add_url_script = self.redis_conn.register_script(
            """
            local url = KEYS[1]
            local domain = ARGV[1]
            local priority = tonumber(ARGV[2])
            local sequence = tonumber(ARGV[3])
            local source_query = ARGV[4]
            local visited_set = ARGV[5]
            local queued_set = ARGV[6]
            local metadata_key = ARGV[7]
            local domain_queue = ARGV[8]

            -- Check if already visited or queued
            if redis.call('sismember', visited_set, url) == 1 then
                return 0
            end
            if redis.call('sismember', queued_set, url) == 1 then
                return 0
            end

            -- Add to queued set
            redis.call('sadd', queued_set, url)

            -- Add to domain queue (sorted set with priority+sequence as score)
            local score = priority * 1000000 + sequence
            redis.call('zadd', domain_queue, score, url)

            -- Store metadata
            redis.call('hset', metadata_key, 'priority', priority, 'sequence', sequence, 'source_query', source_query)

            return 1
            """
        )

        # Lua script for atomic get_next_url operation
        self._get_next_url_script = self.redis_conn.register_script(
            """
            local domain = ARGV[1]
            local rate_limit = tonumber(ARGV[2])
            local now = tonumber(ARGV[3])
            local domain_queue = ARGV[4]
            local domain_next_time_key = ARGV[5]
            local queued_set = ARGV[6]

            -- Check rate limit
            local next_time = redis.call('get', domain_next_time_key)
            if next_time and tonumber(next_time) > now then
                return nil
            end

            -- Get next URL from domain queue (sorted set with priority as score)
            local urls = redis.call('zrange', domain_queue, 0, 0)
            if #urls == 0 then
                return nil
            end

            local url = urls[1]

            -- Atomically remove URL from queue and queued set
            redis.call('zrem', domain_queue, url)
            redis.call('srem', queued_set, url)

            -- Update rate limit
            redis.call('set', domain_next_time_key, math.floor(now + rate_limit))

            return url
            """
        )

        # Lua script for atomic mark_visited operation
        self._mark_visited_script = self.redis_conn.register_script(
            """
            local url = KEYS[1]
            local domain = ARGV[1]
            local queued_set = ARGV[2]
            local visited_set = ARGV[3]
            local domain_queue = ARGV[4]

            -- Remove from queued set
            redis.call('srem', queued_set, url)

            -- Remove from domain queue
            redis.call('zrem', domain_queue, url)

            -- Add to visited set
            redis.call('sadd', visited_set, url)

            return 1
            """
        )

    def _get_redis_key(self, *parts: str) -> str:
        """Build namespaced Redis key."""
        return f"{self.namespace}:" + ":".join(parts)

    def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool:
        """Add a URL to the frontier if not already seen.

        Args:
            url: URL to add
            priority: Priority for crawling (lower = sooner)
            source_query: Optional query that discovered this URL

        Returns:
            True if added, False if already seen
        """
        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            if URLUtils.is_blacklisted(url):
                logger.debug(f"Ignoring blacklisted URL before queueing: {url}")
            return False

        domain = urlparse(cleaned).netloc
        sequence = self.redis_conn.incr(self._get_redis_key("sequence"))

        try:
            result = self._add_url_script(
                keys=[cleaned],
                args=[
                    domain,
                    priority,
                    sequence,
                    source_query or "",
                    self._get_redis_key("urls", "visited"),
                    self._get_redis_key("urls", "queued"),
                    self._get_redis_key("urls", "metadata", cleaned),
                    self._get_redis_key("urls", "domain", domain, "queue"),
                ],
            )

            if result:
                if self.url_database is not None:
                    self.url_database.add_url(cleaned, status="queued")
                logger.debug(f"Added to frontier: {cleaned}")
            return bool(result)

        except redis.RedisError as e:
            logger.error(f"Redis error adding URL: {e}")
            return False

    def get_next_url(self) -> str | None:
        """Return the next crawlable URL, respecting per-domain rate limits."""
        now = time.time()

        try:
            # Get all domains
            domain_pattern = self._get_redis_key("urls", "domain", "*", "queue")
            cursor = 0
            blocked_domains = []

            while True:
                cursor, keys = self.redis_conn.scan(
                    cursor, match=domain_pattern, count=100
                )

                for domain_queue_key in keys:
                    # Extract domain from key
                    parts = domain_queue_key.decode().split(":")
                    domain = parts[-2]  # -2 because last is "queue"

                    domain_next_time_key = self._get_redis_key(
                        "urls", "domain", domain, "next_time"
                    )

                    # Try to get URL atomically
                    url = self._get_next_url_script(
                        args=[
                            domain,
                            self.rate_limit,
                            now,
                            domain_queue_key,
                            domain_next_time_key,
                            self._get_redis_key("urls", "queued"),
                        ],
                    )

                    if url:
                        url_str = url.decode() if isinstance(url, bytes) else url
                        if URLUtils.is_blacklisted(url_str):
                            self.mark_skipped(url_str)
                            logger.info(f"Skipping blacklisted URL: {url_str}")
                            continue

                        if self.url_database is not None:
                            self.url_database.update_status(url_str, "pending")

                        return url_str

                if cursor == 0:
                    break

            return None

        except redis.RedisError as e:
            logger.error(f"Redis error getting next URL: {e}")
            return None

    def mark_visited(self, url: str) -> None:
        """Mark a URL as visited."""
        cleaned = URLUtils.normalize_url(url)
        if not cleaned:
            return

        domain = urlparse(cleaned).netloc

        try:
            self._mark_visited_script(
                keys=[cleaned],
                args=[
                    domain,
                    self._get_redis_key("urls", "queued"),
                    self._get_redis_key("urls", "visited"),
                    self._get_redis_key("urls", "domain", domain, "queue"),
                ],
            )

            if self.url_database is not None:
                self.url_database.update_status(cleaned, "visited")

        except redis.RedisError as e:
            logger.error(f"Redis error marking visited: {e}")

    def mark_failed(self, url: str) -> None:
        """Mark a URL as failed."""
        cleaned = URLUtils.normalize_url(url)
        if not cleaned:
            return

        domain = urlparse(cleaned).netloc

        try:
            # Remove from queued and domain queue
            self.redis_conn.srem(self._get_redis_key("urls", "queued"), cleaned)
            self.redis_conn.zrem(
                self._get_redis_key("urls", "domain", domain, "queue"), cleaned
            )

            # Add to visited (treat failed same as visited for dedup purposes)
            self.redis_conn.sadd(self._get_redis_key("urls", "visited"), cleaned)

            if self.url_database is not None:
                self.url_database.update_status(cleaned, "failed")

        except redis.RedisError as e:
            logger.error(f"Redis error marking failed: {e}")

    def mark_skipped(self, url: str) -> None:
        """Mark a URL as skipped."""
        cleaned = URLUtils.normalize_url(url)
        if not cleaned:
            return

        domain = urlparse(cleaned).netloc

        try:
            self.redis_conn.srem(self._get_redis_key("urls", "queued"), cleaned)
            self.redis_conn.zrem(
                self._get_redis_key("urls", "domain", domain, "queue"), cleaned
            )
            self.redis_conn.sadd(self._get_redis_key("urls", "visited"), cleaned)

            if self.url_database is not None:
                self.url_database.update_status(cleaned, "skipped")

        except redis.RedisError as e:
            logger.error(f"Redis error marking skipped: {e}")

    def has_pending(self) -> bool:
        """Return True when the frontier still has queued work."""
        try:
            count = self.redis_conn.scard(self._get_redis_key("urls", "queued"))
            return count > 0
        except redis.RedisError:
            return False

    def pending_count(self) -> int:
        """Return the number of queued URLs."""
        try:
            return self.redis_conn.scard(self._get_redis_key("urls", "queued"))
        except redis.RedisError:
            return 0

    def get_source_query(self, url: str) -> str:
        """Return the source query for a URL."""
        cleaned = URLUtils.normalize_url(url)
        if not cleaned:
            return ""

        try:
            metadata = self.redis_conn.hget(
                self._get_redis_key("urls", "metadata", cleaned), "source_query"
            )
            return metadata.decode() if metadata else ""
        except redis.RedisError:
            return ""

    def get_status_counts(self) -> dict[str, int]:
        """Get counts of URLs in each status."""
        try:
            return {
                "queued": self.redis_conn.scard(
                    self._get_redis_key("urls", "queued")
                ),
                "visited": self.redis_conn.scard(
                    self._get_redis_key("urls", "visited")
                ),
            }
        except redis.RedisError:
            return {}

    def clear(self) -> None:
        """Clear all frontier state (useful for testing)."""
        try:
            pattern = self._get_redis_key("urls", "*")
            cursor = 0
            while True:
                cursor, keys = self.redis_conn.scan(cursor, match=pattern, count=100)
                if keys:
                    self.redis_conn.delete(*keys)
                if cursor == 0:
                    break
            self.redis_conn.delete(self._get_redis_key("sequence"))
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
