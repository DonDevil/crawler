"""Tests for CrawlerManager's one-shot startup recovery sweep
(docs/architecture/history/redis-startup-recovery.md, following up on
redis-startup-recovery-audit.md).

These are CrawlerManager-level integration tests -- they verify the wiring
(`_run_startup_recovery`, its call site in `run()`, its bounding, its
gating) rather than re-proving `reclaim_and_promote`'s own Lua-level
correctness, which `tests/redis_frontier_test.py::TestClaimLifecycle`
already covers.

Redis-backed: needs a live Redis on localhost:6379 (DB 1, same convention as
tests/redis_frontier_test.py and tests/crawler_manager_recovery_test.py) --
skips if unavailable.
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
        recovery_interval=30.0,
        reclaim_batch_size=200,
        startup_recovery_max_passes=50,
        startup_recovery_max_duration=30.0,
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


def _skip_if_redis_unavailable(manager: CrawlerManager) -> None:
    if not isinstance(manager.frontier, RedisURLFrontier):
        pytest.skip("Redis not available on localhost:6379")


def _force_expire_lease(frontier: RedisURLFrontier, url: str) -> None:
    """Test hook: backdate a URL's inflight lease score into the past,
    simulating an abandoned/crashed worker without waiting out lease_ttl."""
    frontier.redis_conn.zadd(frontier._key("inflight"), {url: 0})


def _make_producer(namespace: str, **overrides) -> RedisURLFrontier:
    """Play the role of a previous/independent crawler process against the
    same Redis namespace. Only called after `_skip_if_redis_unavailable`
    has confirmed Redis is reachable, so construction here should not
    itself raise."""
    kwargs = dict(
        redis_host="localhost",
        redis_port=6379,
        redis_db=1,
        namespace=namespace,
        rate_limit=0,
        max_retries=3,
        base_backoff=0,
    )
    kwargs.update(overrides)
    return RedisURLFrontier(**kwargs)


class TestStartupRecoveryReconciliation:
    @pytest.mark.asyncio
    async def test_startup_recovery_recovers_expired_claim(self, tmp_path):
        namespace = "startup_recover_expired"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            producer = _make_producer(namespace)
            try:
                url = "https://crashed-process.example.com/page"
                producer.add_url(url, priority=10)
                claim = producer.get_next_url()
                assert claim is not None
                _force_expire_lease(producer, url)

                await manager._run_startup_recovery()

                counts = manager.frontier.get_status_counts()
                assert counts["inflight"] == 0
                assert counts["queued"] == 1, "base_backoff=0 -> requeued within the sweep"
            finally:
                producer.close()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_startup_recovery_does_not_reclaim_live_claim(self, tmp_path):
        namespace = "startup_recover_live"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            producer = _make_producer(namespace)
            try:
                url = "https://still-alive.example.com/page"
                producer.add_url(url, priority=10)
                claim = producer.get_next_url()
                assert claim is not None
                # Lease is not force-expired -- this claim is still live.

                await manager._run_startup_recovery()

                counts = manager.frontier.get_status_counts()
                assert counts["inflight"] == 1
                assert counts["queued"] == 0
                assert counts["retry_scheduled"] == 0
            finally:
                producer.close()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_startup_recovery_precedes_worker_claiming(self, tmp_path):
        namespace = "startup_recover_order"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            order = []
            original_reclaim = manager.async_frontier.reclaim_and_promote

            async def recording_reclaim(batch_size=None):
                order.append("recovery")
                return await original_reclaim(batch_size)

            manager.async_frontier.reclaim_and_promote = recording_reclaim

            async def stub_run():
                order.append("get_next_url")
                manager.frontier.get_next_url()

            manager._crawler.run = stub_run

            await manager.run()

            assert "recovery" in order and "get_next_url" in order
            assert order.index("recovery") < order.index("get_next_url"), (
                f"startup recovery must precede the first get_next_url() call, got order={order}"
            )
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_startup_recovery_handles_batch_backlog(self, tmp_path):
        namespace = "startup_recover_backlog"
        small_batch = 3
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace, reclaim_batch_size=small_batch),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            producer = _make_producer(namespace, reclaim_batch_size=small_batch)
            try:
                urls = [f"https://backlog.example.com/page{i}" for i in range(7)]
                for url in urls:
                    producer.add_url(url, priority=10)
                    claim = producer.get_next_url()
                    assert claim is not None
                    _force_expire_lease(producer, url)

                call_count = 0
                original_reclaim = manager.async_frontier.reclaim_and_promote

                async def counting_reclaim(batch_size=None):
                    nonlocal call_count
                    call_count += 1
                    return await original_reclaim(batch_size)

                manager.async_frontier.reclaim_and_promote = counting_reclaim

                await manager._run_startup_recovery()

                assert call_count >= 3, (
                    f"7 expired claims at batch_size=3 must take multiple passes, got {call_count}"
                )
                counts = manager.frontier.get_status_counts()
                assert counts["inflight"] == 0
                assert counts["queued"] == 7
            finally:
                producer.close()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_startup_recovery_handles_due_retry(self, tmp_path):
        namespace = "startup_recover_due_retry"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            producer = _make_producer(namespace)
            try:
                url = "https://due-retry.example.com/page"
                producer.add_url(url, priority=10)
                claim = producer.get_next_url()
                assert claim is not None
                producer.mark_failed(claim, "boom")

                counts = producer.get_status_counts()
                assert counts["retry_scheduled"] == 1
                assert counts["queued"] == 0

                await manager._run_startup_recovery()

                counts = manager.frontier.get_status_counts()
                assert counts["retry_scheduled"] == 0
                assert counts["queued"] == 1

                new_claim = manager.frontier.get_next_url()
                assert new_claim is not None
                assert new_claim.url == url
                assert new_claim.attempt == 2, "attempt continuity across the recovered retry"
            finally:
                producer.close()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_concurrent_startup_recovery_is_safe(self, tmp_path):
        namespace = "startup_recover_concurrent"
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()

        manager_a = CrawlerManager(
            config=_make_redis_config(dir_a, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        manager_b = CrawlerManager(
            config=_make_redis_config(dir_b, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager_a)
            _skip_if_redis_unavailable(manager_b)

            producer = _make_producer(namespace)
            try:
                url = "https://concurrent-crash.example.com/page"
                producer.add_url(url, priority=10)
                claim = producer.get_next_url()
                assert claim is not None
                _force_expire_lease(producer, url)

                totals = {"a": [0, 0], "b": [0, 0]}

                def wrap(mgr: CrawlerManager, key: str) -> None:
                    original = mgr.async_frontier.reclaim_and_promote

                    async def counted(batch_size=None):
                        reclaimed, requeued = await original(batch_size)
                        totals[key][0] += reclaimed
                        totals[key][1] += requeued
                        return reclaimed, requeued

                    mgr.async_frontier.reclaim_and_promote = counted

                wrap(manager_a, "a")
                wrap(manager_b, "b")

                await asyncio.gather(
                    manager_a._run_startup_recovery(),
                    manager_b._run_startup_recovery(),
                )

                total_reclaimed = totals["a"][0] + totals["b"][0]
                assert total_reclaimed == 1, (
                    "the abandoned claim must be reclaimed exactly once across both systems, "
                    f"got a={totals['a'][0]} b={totals['b'][0]}"
                )

                counts = manager_a.frontier.get_status_counts()
                assert counts["inflight"] == 0
                assert counts["queued"] == 1

                claim_a = manager_a.frontier.get_next_url()
                claim_b = manager_b.frontier.get_next_url()
                claimed = [c for c in (claim_a, claim_b) if c is not None]
                assert len(claimed) == 1, "exactly one system must win the reclaimed URL"
            finally:
                producer.close()
        finally:
            manager_a.frontier.clear()
            manager_a.url_database.close()
            manager_a.domain_database.close()
            manager_b.url_database.close()
            manager_b.domain_database.close()

    @pytest.mark.asyncio
    async def test_recovered_attempt_and_fencing(self, tmp_path):
        namespace = "startup_recover_fencing"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            producer = _make_producer(namespace)
            try:
                url = "https://fencing.example.com/page"
                producer.add_url(url, priority=10)
                old_claim = producer.get_next_url()
                assert old_claim is not None
                assert old_claim.attempt == 1
                _force_expire_lease(producer, url)

                await manager._run_startup_recovery()

                new_claim = manager.frontier.get_next_url()
                assert new_claim is not None
                assert new_claim.attempt == 2
                assert new_claim.token != old_claim.token

                manager.frontier.mark_visited(old_claim)
                counts = manager.frontier.get_status_counts()
                assert counts["visited"] == 0, "stale completion from attempt 1 must be rejected"
                assert counts["inflight"] == 1, "the new claim must remain untouched"

                manager.frontier.mark_visited(new_claim)
                counts = manager.frontier.get_status_counts()
                assert counts["visited"] == 1
                assert counts["inflight"] == 0
            finally:
                producer.close()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_startup_recovery_preserves_existing_queued_work(self, tmp_path):
        namespace = "startup_recover_preserve"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            producer = _make_producer(namespace)
            try:
                # Claim and resolve the visited/abandoned URLs *before* the
                # never-claimed queued URLs are added, so each get_next_url()
                # call below is unambiguous about which URL it returns
                # (domain-head ordering across same-priority domains is not
                # otherwise deterministic, and is not what this test is
                # about).
                visited_url = "https://visited.example.com/page"
                producer.add_url(visited_url, priority=10)
                visited_claim = producer.get_next_url()
                assert visited_claim is not None and visited_claim.url == visited_url
                producer.mark_visited(visited_claim)

                abandoned_url = "https://abandoned.example.com/page"
                producer.add_url(abandoned_url, priority=10)
                abandoned_claim = producer.get_next_url()
                assert abandoned_claim is not None and abandoned_claim.url == abandoned_url
                _force_expire_lease(producer, abandoned_url)

                queued_urls = [f"https://queued.example.com/page{i}" for i in range(3)]
                for url in queued_urls:
                    producer.add_url(url, priority=10)

                counts_before = producer.get_status_counts()
                assert counts_before["queued"] == 3
                assert counts_before["visited"] == 1
                assert counts_before["inflight"] == 1

                await manager._run_startup_recovery()

                counts_after = manager.frontier.get_status_counts()
                assert counts_after["queued"] == 4, "3 untouched + the recovered URL"
                assert counts_after["visited"] == 1, "queued/visited work must not be destroyed"
                assert counts_after["inflight"] == 0
            finally:
                producer.close()
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_periodic_recovery_still_runs(self, tmp_path):
        namespace = "startup_recover_periodic_still_runs"
        manager = CrawlerManager(
            config=_make_redis_config(tmp_path, namespace, recovery_interval=0.1),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            call_count = 0
            original_reclaim = manager.async_frontier.reclaim_and_promote

            async def counting_reclaim(batch_size=None):
                nonlocal call_count
                call_count += 1
                return await original_reclaim(batch_size)

            manager.async_frontier.reclaim_and_promote = counting_reclaim

            async def short_running_stub():
                await asyncio.sleep(0.35)

            manager._crawler.run = short_running_stub

            await manager.run()

            assert manager._recovery_task is not None
            assert manager._recovery_task.done()
            assert manager._recovery_task.cancelled()
            assert call_count >= 4, (
                f"expected the startup sweep plus several periodic ticks, got {call_count}"
            )
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()

    @pytest.mark.asyncio
    async def test_startup_recovery_bound(self, tmp_path):
        namespace = "startup_recover_bound"
        manager = CrawlerManager(
            config=_make_redis_config(
                tmp_path,
                namespace,
                startup_recovery_max_passes=5,
                startup_recovery_max_duration=30.0,
            ),
            crawl_engine="http",
            include_seed_files=False,
        )
        try:
            _skip_if_redis_unavailable(manager)

            call_count = 0

            async def never_converging(batch_size=None):
                nonlocal call_count
                call_count += 1
                return (1, 1)

            manager.async_frontier.reclaim_and_promote = never_converging

            await manager._run_startup_recovery()

            assert call_count == 5, (
                f"startup recovery must stop at the configured pass bound, got {call_count}"
            )
        finally:
            manager.frontier.clear()
            manager.url_database.close()
            manager.domain_database.close()
