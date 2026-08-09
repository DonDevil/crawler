"""Tests for core/claim_heartbeat.py (docs/architecture/frontier-adr.md §8,
migration Step 5).

Covers `run_with_heartbeat` directly against both frontier backends: the
local in-memory `URLFrontier` (heartbeat calls run inline, no thread) and
`RedisURLFrontier` (heartbeat calls must be routed through `AsyncFrontier`'s
`asyncio.to_thread` offload -- see docs/architecture/frontier-step4.md).
Crawler-backend-level integration (per-worker `ClaimLostError` handling) is
covered separately in tests/crawler_test.py and tests/hybrid_crawler_test.py.

The Redis-backed tests need a live Redis on localhost:6379 and are skipped
otherwise, consistent with tests/redis_frontier_test.py.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import redis

from core.claim_heartbeat import (
    ClaimLostError,
    default_heartbeat_interval,
    resolve_heartbeat_interval,
    run_with_heartbeat,
)
from core.frontier_executor import AsyncFrontier
from core.redis_frontier import RedisURLFrontier
from core.url_frontier import URLFrontier


def _current_thread_id() -> int:
    return threading.get_ident()


def _force_expire_lease(frontier: RedisURLFrontier, url: str) -> None:
    """Test hook shared with tests/redis_frontier_test.py: backdate a URL's
    inflight lease score into the past, simulating an abandoned/expired
    lease without waiting out lease_ttl."""
    frontier.redis_conn.zadd(frontier._key("inflight"), {url: 0})


@pytest.fixture
def redis_frontier() -> RedisURLFrontier:
    try:
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="step5_heartbeat_test",
            rate_limit=0,
            base_backoff=0,
        )
        frontier.clear()
        yield frontier
        frontier.clear()
        frontier.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


class TestIntervalDerivation:
    def test_default_interval_is_a_third_of_lease_ttl(self):
        assert default_heartbeat_interval(90.0) == 30.0
        assert default_heartbeat_interval(9.0) == 3.0

    def test_default_interval_has_a_floor(self):
        # Floor is intentionally small (see core/claim_heartbeat.py's
        # _MIN_HEARTBEAT_INTERVAL docstring) -- it must never dominate over
        # a legitimately short lease_ttl.
        assert default_heartbeat_interval(1.0) == pytest.approx(1.0 / 3.0)
        assert default_heartbeat_interval(0.1) == pytest.approx(0.05)
        assert default_heartbeat_interval(0.0001) == pytest.approx(0.05)

    def test_default_interval_falls_back_for_invalid_lease_ttl(self):
        assert default_heartbeat_interval(0) == default_heartbeat_interval()
        assert default_heartbeat_interval(-5) == default_heartbeat_interval()

    def test_resolve_uses_default_when_unconfigured(self):
        assert resolve_heartbeat_interval(None, 90.0) == 30.0
        assert resolve_heartbeat_interval(0, 90.0) == 30.0
        assert resolve_heartbeat_interval(-1, 90.0) == 30.0

    def test_resolve_clamps_an_explicit_override_below_lease_ttl(self):
        # 200 would exceed (indeed dwarf) a 90s lease -- must be clamped,
        # never honored as-is (requirement: interval can never reach/exceed
        # the lease it's supposed to keep alive).
        resolved = resolve_heartbeat_interval(200, 90.0)
        assert resolved < 90.0
        assert resolved == 45.0  # lease_ttl / 2 safety ceiling

    def test_resolve_honors_a_safe_explicit_override(self):
        assert resolve_heartbeat_interval(5.0, 90.0) == 5.0


class TestLocalFrontierHeartbeat:
    @pytest.mark.asyncio
    async def test_normal_heartbeat_keeps_claim_alive_and_renews_lease(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/slow")
        claim = frontier.get_next_url()
        assert claim is not None
        original_expiry = claim.lease_expires_at

        adapter = AsyncFrontier(frontier)

        async def slow_work():
            await asyncio.sleep(0.15)
            return "done"

        result, renewed_claim = await run_with_heartbeat(
            adapter, claim, slow_work(), heartbeat_interval=1.0
        )

        assert result == "done"
        # heartbeat_interval=1.0 > 0.15s sleep means no renewal tick actually
        # fired before the work finished -- claim is unchanged, and the
        # underlying frontier still recognizes it as current (still
        # completable), proving nothing broke it.
        assert renewed_claim.token == claim.token
        await adapter.mark_visited(renewed_claim)
        assert "https://example.com/slow" in frontier.visited

    @pytest.mark.asyncio
    async def test_heartbeat_actually_renews_and_updates_lease_expiry(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/renews")
        claim = frontier.get_next_url()
        adapter = AsyncFrontier(frontier)

        async def work_spanning_two_ticks():
            await asyncio.sleep(0.25)
            return "ok"

        result, final_claim = await run_with_heartbeat(
            adapter, claim, work_spanning_two_ticks(), heartbeat_interval=0.1
        )

        assert result == "ok"
        assert final_claim.token == claim.token
        assert final_claim.lease_expires_at > claim.lease_expires_at, (
            "at least one heartbeat tick should have renewed the lease"
        )

    @pytest.mark.asyncio
    async def test_stale_claim_cannot_be_renewed_and_raises_claim_lost(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/stale")
        claim = frontier.get_next_url()
        assert claim is not None

        # Complete the claim out from under the heartbeat -- simulates
        # another owner (or, on Redis, a recovery sweep) already resolving
        # this URL, making `claim`'s token stale.
        frontier.mark_visited(claim)

        adapter = AsyncFrontier(frontier)

        async def slow_work():
            await asyncio.sleep(1.0)
            return "should never get here"

        with pytest.raises(ClaimLostError) as exc_info:
            await run_with_heartbeat(adapter, claim, slow_work(), heartbeat_interval=0.05)

        assert exc_info.value.claim is claim

    @pytest.mark.asyncio
    async def test_work_exception_propagates_and_stops_heartbeat(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/boom")
        claim = frontier.get_next_url()
        adapter = AsyncFrontier(frontier)

        async def failing_work():
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await run_with_heartbeat(adapter, claim, failing_work(), heartbeat_interval=0.02)

        # Claim is still valid afterward (heartbeat didn't corrupt it) --
        # the caller (worker()) is expected to call mark_failed itself.
        frontier.mark_failed(claim, "boom")
        assert frontier.get_status_counts()["inflight"] == 0

    @pytest.mark.asyncio
    async def test_outer_cancellation_stops_heartbeat_and_drains_work_task(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/cancelme")
        claim = frontier.get_next_url()
        adapter = AsyncFrontier(frontier)

        work_task_cancelled = asyncio.Event()

        async def hang_forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                work_task_cancelled.set()
                raise

        async def run():
            await run_with_heartbeat(adapter, claim, hang_forever(), heartbeat_interval=0.05)

        outer_task = asyncio.create_task(run())
        await asyncio.sleep(0.1)
        outer_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await outer_task

        # Give the cancellation a beat to propagate into hang_forever().
        await asyncio.wait_for(work_task_cancelled.wait(), timeout=1.0)
        assert work_task_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_no_orphan_task_remains_after_normal_completion(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/clean")
        claim = frontier.get_next_url()
        adapter = AsyncFrontier(frontier)

        tasks_before = asyncio.all_tasks()

        async def quick_work():
            return "done"

        await run_with_heartbeat(adapter, claim, quick_work(), heartbeat_interval=0.05)

        # Let any stray callback-scheduled task get a chance to run/finish.
        await asyncio.sleep(0)
        new_tasks = asyncio.all_tasks() - tasks_before
        assert not new_tasks, f"orphan tasks left running: {new_tasks}"

    @pytest.mark.asyncio
    async def test_no_orphan_task_remains_after_claim_lost(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/lost")
        claim = frontier.get_next_url()
        frontier.mark_visited(claim)  # invalidate before heartbeat starts
        adapter = AsyncFrontier(frontier)

        tasks_before = asyncio.all_tasks()

        async def slow_work():
            await asyncio.sleep(5)

        with pytest.raises(ClaimLostError):
            await run_with_heartbeat(adapter, claim, slow_work(), heartbeat_interval=0.02)

        await asyncio.sleep(0)
        new_tasks = asyncio.all_tasks() - tasks_before
        assert not new_tasks, f"orphan tasks left running: {new_tasks}"

    @pytest.mark.asyncio
    async def test_multiple_workers_renewing_different_claims_do_not_interfere(self):
        frontier = URLFrontier(rate_limit=0, lease_ttl=90.0)
        frontier.add_url("https://example.com/a")
        frontier.add_url("https://example.com/b")
        claim_a = frontier.get_next_url()
        claim_b = frontier.get_next_url()
        assert claim_a is not None and claim_b is not None
        assert claim_a.url != claim_b.url

        adapter = AsyncFrontier(frontier)

        async def work(tag, delay):
            await asyncio.sleep(delay)
            return tag

        (result_a, final_a), (result_b, final_b) = await asyncio.gather(
            run_with_heartbeat(adapter, claim_a, work("a", 0.2), heartbeat_interval=0.05),
            run_with_heartbeat(adapter, claim_b, work("b", 0.2), heartbeat_interval=0.05),
        )

        assert result_a == "a"
        assert result_b == "b"
        assert final_a.token == claim_a.token
        assert final_b.token == claim_b.token

        await adapter.mark_visited(final_a)
        await adapter.mark_visited(final_b)
        assert "https://example.com/a" in frontier.visited
        assert "https://example.com/b" in frontier.visited


class TestRedisFrontierHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_keeps_claim_alive_beyond_original_lease_ttl(
        self, redis_frontier: RedisURLFrontier
    ):
        redis_frontier.lease_ttl = 0.3
        url = "https://heartbeat.example.com/slow"
        redis_frontier.add_url(url, priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        adapter = AsyncFrontier(redis_frontier)

        async def slow_fetch():
            # Runs well past the original 0.3s lease -- without heartbeating,
            # a recovery sweep would find this claim expired and reclaim it.
            await asyncio.sleep(0.8)
            return "fetched"

        result, final_claim = await run_with_heartbeat(
            adapter, claim, slow_fetch(), heartbeat_interval=0.1
        )
        assert result == "fetched"
        assert final_claim.lease_expires_at > claim.lease_expires_at, (
            "expected at least one renewal to have pushed the lease forward"
        )

        # No recovery sweep ran, and the claim is still ours: completing it
        # must succeed (a reclaimed claim would make this a silent no-op).
        await adapter.mark_visited(final_claim)
        counts = redis_frontier.get_status_counts()
        assert counts["visited"] == 1
        assert counts["inflight"] == 0

    @pytest.mark.asyncio
    async def test_without_heartbeat_a_slow_fetch_would_be_reclaimed(
        self, redis_frontier: RedisURLFrontier
    ):
        """Negative control proving the above test is meaningful: the exact
        same slow fetch, with no heartbeat, does get reclaimed."""
        redis_frontier.lease_ttl = 0.3
        url = "https://heartbeat.example.com/no-heartbeat"
        redis_frontier.add_url(url, priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        await asyncio.sleep(0.5)  # outlive the lease with no renewal
        reclaimed, requeued = redis_frontier.reclaim_and_promote()
        assert reclaimed == 1
        assert requeued == 1

        # Original worker's completion is now rejected -- proves the claim
        # really was reclaimed, not just still sitting there.
        redis_frontier.mark_visited(claim)
        assert redis_frontier.get_status_counts()["visited"] == 0

    @pytest.mark.asyncio
    async def test_redis_renewal_calls_do_not_run_on_the_event_loop_thread(
        self, redis_frontier: RedisURLFrontier
    ):
        url = "https://heartbeat.example.com/thread-check"
        redis_frontier.add_url(url, priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        loop_thread = _current_thread_id()
        renewal_threads: list[int] = []

        original_renew = redis_frontier.renew_claim

        def spying_renew(*args, **kwargs):
            renewal_threads.append(_current_thread_id())
            return original_renew(*args, **kwargs)

        redis_frontier.renew_claim = spying_renew
        adapter = AsyncFrontier(redis_frontier)

        async def work_spanning_two_ticks():
            await asyncio.sleep(0.25)
            return "ok"

        await run_with_heartbeat(adapter, claim, work_spanning_two_ticks(), heartbeat_interval=0.1)

        assert renewal_threads, "expected at least one renew_claim call"
        assert all(t != loop_thread for t in renewal_threads), (
            "renew_claim ran on the event-loop thread"
        )

    @pytest.mark.asyncio
    async def test_claim_reclaimed_by_another_worker_causes_heartbeat_to_fail_safely(
        self, redis_frontier: RedisURLFrontier
    ):
        url = "https://heartbeat.example.com/reclaimed"
        redis_frontier.add_url(url, priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        adapter = AsyncFrontier(redis_frontier)

        async def slow_fetch():
            await asyncio.sleep(2.0)
            return "should never complete"

        run_task = asyncio.create_task(
            run_with_heartbeat(adapter, claim, slow_fetch(), heartbeat_interval=0.1)
        )
        await asyncio.sleep(0.05)  # let it start heartbeating

        # Simulate the Step 4 recovery task reclaiming this worker's lease
        # out from under it (crashed-worker scenario) while it's still
        # "working".
        _force_expire_lease(redis_frontier, url)
        reclaimed, requeued = redis_frontier.reclaim_and_promote()
        assert reclaimed == 1

        with pytest.raises(ClaimLostError):
            await run_task

        # The reclaimed URL is claimable again by a new worker.
        new_claim = redis_frontier.get_next_url()
        assert new_claim is not None
        assert new_claim.token != claim.token

    @pytest.mark.asyncio
    async def test_overlapping_renewals_for_same_claim_do_not_happen(
        self, redis_frontier: RedisURLFrontier
    ):
        """A slow renew_claim round trip must not let a second renewal for
        the same claim start before the first returns."""
        url = "https://heartbeat.example.com/no-overlap"
        redis_frontier.add_url(url, priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        adapter = AsyncFrontier(redis_frontier)
        original_renew = redis_frontier.renew_claim
        concurrent = 0
        max_concurrent = 0
        lock = threading.Lock()

        def slow_renew(*args, **kwargs):
            nonlocal concurrent, max_concurrent
            with lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.15)
            try:
                return original_renew(*args, **kwargs)
            finally:
                with lock:
                    concurrent -= 1

        redis_frontier.renew_claim = slow_renew

        async def work_spanning_several_ticks():
            await asyncio.sleep(0.5)
            return "ok"

        await run_with_heartbeat(
            adapter, claim, work_spanning_several_ticks(), heartbeat_interval=0.05
        )

        assert max_concurrent == 1, "overlapping renewals detected for the same claim"

    @pytest.mark.asyncio
    async def test_multiple_workers_renewing_different_redis_claims_do_not_interfere(
        self, redis_frontier: RedisURLFrontier
    ):
        redis_frontier.add_url("https://heartbeat.example.com/multi-a", priority=10)
        redis_frontier.add_url("https://heartbeat.example.com/multi-b", priority=10)
        claim_a = redis_frontier.get_next_url()
        claim_b = redis_frontier.get_next_url()
        assert claim_a is not None and claim_b is not None

        adapter = AsyncFrontier(redis_frontier)

        async def work(delay):
            await asyncio.sleep(delay)
            return "done"

        (result_a, final_a), (result_b, final_b) = await asyncio.gather(
            run_with_heartbeat(adapter, claim_a, work(0.3), heartbeat_interval=0.05),
            run_with_heartbeat(adapter, claim_b, work(0.3), heartbeat_interval=0.05),
        )

        assert result_a == "done" and result_b == "done"
        await adapter.mark_visited(final_a)
        await adapter.mark_visited(final_b)
        counts = redis_frontier.get_status_counts()
        assert counts["visited"] == 2
        assert counts["inflight"] == 0
