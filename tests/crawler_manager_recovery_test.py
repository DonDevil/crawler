"""Tests for the Step 4 asyncio background recovery task wired into
core/crawler_manager.py (docs/architecture/frontier-adr.md §7,
docs/architecture/frontier-step4.md).

Redis-backed tests need a live Redis on localhost:6379 -- if CrawlerManager
falls back to the local frontier (Redis unreachable), those tests skip,
consistent with tests/redis_frontier_test.py's skip-if-unavailable pattern.
"""

import asyncio

import pytest

from core.config import Config, CrawlerConfig, FrontierConfig, SearchConfig, StorageConfig
from core.crawler_manager import CrawlerManager
from core.redis_frontier import RedisURLFrontier


def _make_redis_config(tmp_path, namespace: str, **frontier_overrides) -> Config:
    sqlite_path = tmp_path / "crawl.db"
    frontier_kwargs = dict(
        type="redis",
        redis_host="localhost",
        redis_port=6379,
        redis_db=1,
        redis_namespace=namespace,
        max_retries=3,
        base_backoff=0,
        max_backoff=1,
        lease_ttl=90.0,
        recovery_enabled=True,
        recovery_interval=0.1,
        reclaim_batch_size=200,
    )
    frontier_kwargs.update(frontier_overrides)

    return Config(
        crawler=CrawlerConfig(
            storage=StorageConfig(sqlite_path=str(sqlite_path)),
            max_pages=1,
            rate_limit=0,
            frontier=FrontierConfig(**frontier_kwargs),
        ),
        search=SearchConfig(enabled_engines=[]),
    )


def _make_local_config(tmp_path, **frontier_overrides) -> Config:
    sqlite_path = tmp_path / "crawl.db"
    return Config(
        crawler=CrawlerConfig(
            storage=StorageConfig(sqlite_path=str(sqlite_path)),
            max_pages=1,
            frontier=FrontierConfig(type="sqlite", **frontier_overrides),
        ),
        search=SearchConfig(enabled_engines=[]),
    )


def _skip_if_redis_unavailable(manager: CrawlerManager) -> None:
    if not isinstance(manager.frontier, RedisURLFrontier):
        pytest.skip("Redis not available on localhost:6379")


class TestRecoveryTaskGating:
    """The recovery task should only exist when it has something to do."""

    @pytest.mark.asyncio
    async def test_no_recovery_task_for_local_frontier(self, tmp_path):
        manager = CrawlerManager(
            config=_make_local_config(tmp_path),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:

            async def fast_stub():
                return None

            manager._crawler.run = fast_stub
            await manager.run()

            assert manager._recovery_task is None
        finally:
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_no_recovery_task_when_recovery_disabled(self, tmp_path):
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, "step4_gate_disabled", recovery_enabled=False),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            async def fast_stub():
                return None

            manager._crawler.run = fast_stub
            await manager.run()

            assert manager._recovery_task is None
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()


class TestRecoveryTaskBehavior:
    @pytest.mark.asyncio
    async def test_recovery_loop_calls_reclaim_and_promote_periodically(self, tmp_path):
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, "step4_periodic_calls"),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            call_count = 0
            original = manager.async_frontier.reclaim_and_promote

            async def counting_reclaim(batch_size=None):
                nonlocal call_count
                call_count += 1
                return await original(batch_size)

            manager.async_frontier.reclaim_and_promote = counting_reclaim

            task = asyncio.create_task(manager._recovery_loop())
            await asyncio.sleep(0.45)  # ~4-5 ticks at recovery_interval=0.1s
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            assert call_count >= 3, f"expected several recovery sweeps, got {call_count}"
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_retry_scheduled_urls_are_promoted_automatically_without_manual_calls(
        self, tmp_path
    ):
        """The core Step 4 promise: once the recovery task is running, a
        retryable failure eventually becomes claimable again with no test
        code calling reclaim_and_promote directly."""
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, "step4_auto_promote"),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            manager.frontier.add_url("https://retry-promote.example.com/page", priority=10)
            claim = manager.frontier.get_next_url()
            assert claim is not None
            manager.frontier.mark_failed(claim, "boom")

            counts = manager.frontier.get_status_counts()
            assert counts["retry_scheduled"] == 1
            assert counts["queued"] == 0

            task = asyncio.create_task(manager._recovery_loop())
            try:
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    counts = manager.frontier.get_status_counts()
                    if counts["queued"] == 1 and counts["retry_scheduled"] == 0:
                        break
                else:
                    pytest.fail(f"retry-scheduled URL was never auto-promoted: {counts}")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            new_claim = manager.frontier.get_next_url()
            assert new_claim is not None
            assert new_claim.url == "https://retry-promote.example.com/page"
            assert new_claim.attempt == 2
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_recovery_task_shuts_down_cleanly_with_crawler(self, tmp_path):
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, "step4_shutdown"),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            async def short_running_stub():
                await asyncio.sleep(0.3)

            manager._crawler.run = short_running_stub
            await manager.run()

            assert manager._recovery_task is not None
            assert manager._recovery_task.done()
            assert manager._recovery_task.cancelled()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()
