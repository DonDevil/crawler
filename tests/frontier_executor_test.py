"""Tests for core/frontier_executor.py's AsyncFrontier adapter.

Demonstrates the Step 4 requirement directly: Redis frontier operations
invoked from async crawler code must not execute on the event-loop thread,
while local in-memory frontier operations should run inline with no thread
overhead. See docs/architecture/frontier-step4.md.

The Redis-backed tests need a live Redis on localhost:6379 and are skipped
otherwise, consistent with tests/redis_frontier_test.py.
"""

import asyncio
import threading

import pytest
import redis

from core.frontier_executor import AsyncFrontier
from core.redis_frontier import RedisURLFrontier
from core.url_frontier import URLFrontier


def _current_thread_id() -> int:
    return threading.get_ident()


class TestLocalFrontierRunsInline:
    """URLFrontier is pure in-memory -- AsyncFrontier must not offload it."""

    def test_offload_flag_is_false_for_local_frontier(self):
        frontier = URLFrontier(rate_limit=0)
        adapter = AsyncFrontier(frontier)
        assert adapter._offload is False

    @pytest.mark.asyncio
    async def test_local_frontier_calls_run_on_the_calling_thread(self):
        frontier = URLFrontier(rate_limit=0)
        adapter = AsyncFrontier(frontier)
        caller_thread = _current_thread_id()

        await adapter.add_url("https://example.com/a")
        assert _current_thread_id() == caller_thread

        claim = await adapter.get_next_url()
        assert claim is not None
        assert _current_thread_id() == caller_thread

        renewed = await adapter.renew_claim(claim)
        assert renewed is not None
        assert _current_thread_id() == caller_thread

        await adapter.mark_visited(claim)
        assert _current_thread_id() == caller_thread

        counts = await adapter.get_status_counts()
        assert counts["visited"] == 1
        assert _current_thread_id() == caller_thread

    @pytest.mark.asyncio
    async def test_reclaim_and_promote_is_a_safe_no_op_for_local_frontier(self):
        frontier = URLFrontier(rate_limit=0)
        adapter = AsyncFrontier(frontier)
        # URLFrontier has no reclaim_and_promote (ADR §10) -- the adapter
        # must not raise, just report nothing done.
        assert await adapter.reclaim_and_promote() == (0, 0)


@pytest.fixture
def redis_frontier() -> RedisURLFrontier:
    try:
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="step4_executor_test",
            rate_limit=0,
            base_backoff=0,
        )
        frontier.clear()
        yield frontier
        frontier.clear()
        frontier.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


class TestRedisFrontierIsOffloaded:
    """RedisURLFrontier does blocking network I/O -- AsyncFrontier must run
    every operation off the event-loop thread."""

    def test_offload_flag_is_true_for_redis_frontier(self, redis_frontier: RedisURLFrontier):
        adapter = AsyncFrontier(redis_frontier)
        assert adapter._offload is True

    @pytest.mark.asyncio
    async def test_every_redis_operation_runs_off_the_event_loop_thread(
        self, redis_frontier: RedisURLFrontier
    ):
        """Wrap each underlying synchronous frontier method with a thread-
        recording spy (rather than instrumenting the Redis client, which
        has multiple internal code paths -- plain commands vs. pipelines --
        that don't all funnel through one hookable choke point) and assert
        the adapter never invokes it on the event-loop thread."""
        adapter = AsyncFrontier(redis_frontier)
        loop_thread = _current_thread_id()
        recorded: dict[str, int] = {}

        method_names = [
            "add_url",
            "get_next_url",
            "renew_claim",
            "has_pending",
            "pending_count",
            "get_status_counts",
            "get_source_query",
            "reclaim_and_promote",
            "mark_visited",
            "mark_failed",
            "mark_skipped",
        ]
        for name in method_names:
            original = getattr(redis_frontier, name)

            def make_spy(original_method, key):
                def spy(*args, **kwargs):
                    recorded[key] = _current_thread_id()
                    return original_method(*args, **kwargs)

                return spy

            setattr(redis_frontier, name, make_spy(original, name))

        async def assert_offloaded(name, coro):
            recorded.pop(name, None)
            result = await coro
            assert name in recorded, f"{name} was never invoked"
            assert recorded[name] != loop_thread, f"{name} ran on the event-loop thread"
            return result

        await assert_offloaded("add_url", adapter.add_url("https://example.com/probe"))
        claim = await assert_offloaded("get_next_url", adapter.get_next_url())
        assert claim is not None

        renewed = await assert_offloaded("renew_claim", adapter.renew_claim(claim))
        assert renewed is not None

        await assert_offloaded("has_pending", adapter.has_pending())
        await assert_offloaded("pending_count", adapter.pending_count())
        await assert_offloaded("get_status_counts", adapter.get_status_counts())
        await assert_offloaded("get_source_query", adapter.get_source_query(claim.url))
        await assert_offloaded("reclaim_and_promote", adapter.reclaim_and_promote())
        await assert_offloaded("mark_visited", adapter.mark_visited(claim))

        # mark_failed / mark_skipped need their own claim (mark_visited above
        # already completed `claim`).
        await adapter.add_url("https://example.com/probe2")
        claim2 = await adapter.get_next_url()
        await assert_offloaded("mark_failed", adapter.mark_failed(claim2, "boom"))

        await adapter.add_url("https://example.com/probe3")
        claim3 = await adapter.get_next_url()
        await assert_offloaded("mark_skipped", adapter.mark_skipped(claim3))

    @pytest.mark.asyncio
    async def test_concurrent_redis_calls_use_a_bounded_shared_thread_pool(
        self, redis_frontier: RedisURLFrontier
    ):
        """Guards against thread-per-call/unbounded thread growth: firing
        many concurrent offloaded calls must reuse a small, bounded set of
        worker threads (asyncio.to_thread's shared default executor), not
        spawn one OS thread per call."""
        adapter = AsyncFrontier(redis_frontier)
        n_calls = 100

        for i in range(n_calls):
            redis_frontier.add_url(f"https://example.com/bulk{i}")

        seen_threads: set[int] = set()
        lock = asyncio.Lock()

        original_execute_command = redis_frontier.redis_conn.execute_command

        def spying_execute_command(*args, **kwargs):
            seen_threads.add(_current_thread_id())
            return original_execute_command(*args, **kwargs)

        redis_frontier.redis_conn.execute_command = spying_execute_command

        results = await asyncio.gather(*(adapter.get_next_url() for _ in range(n_calls)))
        assert all(claim is not None for claim in results)
        assert len({c.url for c in results}) == n_calls, "every claim must be for a distinct URL"

        # Python's default asyncio to_thread pool caps at min(32, cpu_count+4);
        # well under n_calls, proving these 100 calls were not each given
        # their own thread.
        assert 0 < len(seen_threads) <= 32, (
            f"expected a small bounded set of worker threads, saw {len(seen_threads)}"
        )


class TestAsyncFrontierIdempotency:
    def test_wrapping_an_already_wrapped_frontier_reuses_the_underlying_object(self):
        frontier = URLFrontier(rate_limit=0)
        once = AsyncFrontier(frontier)
        twice = AsyncFrontier(once)

        assert twice._frontier is frontier
        assert twice._offload == once._offload
