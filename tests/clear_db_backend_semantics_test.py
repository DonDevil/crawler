"""Regression tests for backend-aware --clear-db semantics
(core/crawler_manager.py::CrawlerManager.clear_storage).

See docs/architecture/history/clear-db-backend-semantics.md and the audit
that preceded it, docs/architecture/history/clear-db-redis-gap-audit.md:
clear_storage() used to unconditionally clear SQLite regardless of which
frontier backend was actually configured, so a Redis-backed run's
`--clear-db` never reached Redis at all.

Redis-backed tests need a live Redis on localhost:6379 -- if CrawlerManager
falls back to the local frontier (Redis unreachable), those tests skip,
consistent with tests/redis_frontier_test.py's skip-if-unavailable pattern.
Uses redis_db=1 (never the production db 0) with a unique namespace per
test, and never asserts against the production `crawler` namespace.
"""

import uuid

import pytest
import redis as redis_lib

from core.config import Config, CrawlerConfig, FrontierConfig, SearchConfig, StorageConfig
from core.crawler_manager import CrawlerManager
from core.redis_frontier import RedisURLFrontier


def _storage_config(tmp_path) -> StorageConfig:
    # Isolate both the frontier's SQLite file AND the media-evidence
    # SQLite file under tmp_path -- otherwise StorageConfig's defaults
    # ("storage/crawl_state.db" / "storage/media_evidence.db", relative to
    # cwd) point at the real project databases, and clear_storage() would
    # clear production data as a side effect of running these tests.
    return StorageConfig(
        sqlite_path=str(tmp_path / "crawl.db"),
        media_sqlite_path=str(tmp_path / "media_evidence.db"),
        enable_media_evidence=False,
    )


def _make_redis_config(tmp_path, namespace: str) -> Config:
    return Config(
        crawler=CrawlerConfig(
            storage=_storage_config(tmp_path),
            max_pages=1,
            rate_limit=0,
            frontier=FrontierConfig(
                type="redis",
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                redis_namespace=namespace,
                max_retries=3,
                base_backoff=0,
                max_backoff=1,
            ),
        ),
        search=SearchConfig(enabled_engines=[]),
    )


def _make_local_config(tmp_path) -> Config:
    return Config(
        crawler=CrawlerConfig(
            storage=_storage_config(tmp_path),
            max_pages=1,
            frontier=FrontierConfig(type="sqlite"),
        ),
        search=SearchConfig(enabled_engines=[]),
    )


def _make_manager(config, tmp_path):
    return CrawlerManager(
        config=config,
        crawl_engine="http",
        include_seed_files=False,
    )


def _skip_if_redis_unavailable(manager: CrawlerManager) -> None:
    if not isinstance(manager.frontier, RedisURLFrontier):
        manager.url_database.close()
        manager.domain_database.close()
        pytest.skip("Redis not available on localhost:6379")


def _unique_namespace(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _close(manager: CrawlerManager) -> None:
    if hasattr(manager.frontier, "close"):
        manager.frontier.close()
    manager.url_database.close()
    manager.domain_database.close()


class TestClearDbSqliteBackend:
    def test_clear_db_clears_sqlite_crawler_state(self, tmp_path):
        manager = _make_manager(_make_local_config(tmp_path), tmp_path)
        try:
            manager.url_database.add_url("https://example.com/a", status="queued")
            manager.domain_database.add_or_update("example.com", score=1.0)
            assert manager.url_database.get_status_counts() == {"queued": 1}

            manager.clear_storage()

            assert manager.url_database.get_status_counts() == {}
            assert list(manager.domain_database.list_domains()) == []
        finally:
            _close(manager)

    def test_clear_db_does_not_touch_redis(self, tmp_path):
        namespace = _unique_namespace("clear-db-sqlite-mode")
        conn = redis_lib.Redis(host="localhost", port=6379, db=1)
        try:
            conn.ping()
        except redis_lib.RedisError:
            pytest.skip("Redis not available on localhost:6379")

        conn.set(f"{namespace}:known", "sentinel")
        try:
            manager = _make_manager(_make_local_config(tmp_path), tmp_path)
            try:
                manager.url_database.add_url("https://example.com/a", status="queued")
                manager.clear_storage()
            finally:
                _close(manager)

            assert conn.get(f"{namespace}:known") == b"sentinel"
        finally:
            conn.delete(f"{namespace}:known")
            conn.close()

    def test_without_clear_db_sqlite_state_is_untouched(self, tmp_path):
        manager = _make_manager(_make_local_config(tmp_path), tmp_path)
        try:
            manager.url_database.add_url("https://example.com/a", status="queued")
            assert manager.url_database.get_status_counts() == {"queued": 1}
            # clear_storage() intentionally not called.
            assert manager.url_database.get_status_counts() == {"queued": 1}
        finally:
            _close(manager)


class TestClearDbRedisBackend:
    def test_clear_db_clears_redis_namespace(self, tmp_path):
        namespace = _unique_namespace("clear-db-redis")
        manager = _make_manager(_make_redis_config(tmp_path, namespace), tmp_path)
        try:
            _skip_if_redis_unavailable(manager)

            assert manager.frontier.add_url("https://example.com/a", priority=5)
            counts_before = manager.frontier.get_status_counts()
            assert counts_before["queued"] == 1

            manager.clear_storage()

            counts_after = manager.frontier.get_status_counts()
            assert all(count == 0 for count in counts_after.values())
        finally:
            _close(manager)

    def test_clear_db_does_not_unnecessarily_clear_sqlite(self, tmp_path):
        namespace = _unique_namespace("clear-db-redis-sqlite-guard")
        manager = _make_manager(_make_redis_config(tmp_path, namespace), tmp_path)
        try:
            _skip_if_redis_unavailable(manager)

            # Simulates a URL deferred to SQLite during a prior Redis outage
            # (CrawlerManager._make_seed_url_adder's fallback path).
            manager.url_database.add_url("https://deferred.example.com", status="queued")

            manager.clear_storage()

            assert manager.url_database.get_status_counts() == {"queued": 1}
        finally:
            _close(manager)

    def test_without_clear_db_redis_state_is_untouched(self, tmp_path):
        namespace = _unique_namespace("clear-db-redis-noop")
        manager = _make_manager(_make_redis_config(tmp_path, namespace), tmp_path)
        try:
            _skip_if_redis_unavailable(manager)

            assert manager.frontier.add_url("https://example.com/a", priority=5)
            counts_before = manager.frontier.get_status_counts()
            # clear_storage() intentionally not called.
            assert manager.frontier.get_status_counts() == counts_before
            assert counts_before["queued"] == 1
        finally:
            _close(manager)
