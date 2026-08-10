"""Tests for the Redis outage failure contract.

See docs/architecture/frontier-redis-failure-semantics.md for the full
writeup. The core guarantee under test: a Redis infrastructure failure must
never be represented as legitimate empty/zero/false frontier state, and the
crawler scheduler must never read `FrontierUnavailable` as "no more work."

Uses Redis DB 2 with a dedicated namespace -- reserved for this suite so it
never collides with tests/redis_frontier_test.py (DB 1) or production (DB 0).
Redis errors are injected deterministically by patching the specific Lua
`Script` object or `pipeline()` call each frontier method uses, rather than
by killing a real Redis process (per the task's stated preference for
deterministic failure injection).

Run this test ONLY if Redis is available locally on port 6379.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import redis

from core.frontier import FrontierUnavailable
from core.redis_frontier import RedisURLFrontier
from crawler.async_crawler import AsyncCrawler


@pytest.fixture
def redis_frontier() -> RedisURLFrontier:
    """Create a Redis frontier on the dedicated failure-injection DB/namespace."""
    try:
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=2,  # Reserved for this suite -- never DB 0 (production).
            namespace="test_failure_semantics",
            rate_limit=0,
            base_backoff=0,
        )
        frontier.clear()
        yield frontier
        frontier.clear()
        frontier.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


class TestHasPendingAndStatusCountsNeverFakeEmpty:
    """Test 1 & 2: has_pending / get_status_counts must raise, not report
    "nothing pending", when Redis is unreachable."""

    def test_has_pending_raises_instead_of_returning_false(self, redis_frontier: RedisURLFrontier):
        redis_frontier.add_url("https://example.com/a", priority=10)

        with patch.object(redis_frontier.redis_conn, "pipeline", side_effect=redis.ConnectionError("simulated outage")):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.has_pending()

    def test_get_status_counts_raises_instead_of_returning_zeros(self, redis_frontier: RedisURLFrontier):
        redis_frontier.add_url("https://example.com/a", priority=10)

        with patch.object(redis_frontier.redis_conn, "pipeline", side_effect=redis.ConnectionError("simulated outage")):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.get_status_counts()

    def test_pending_count_raises_too(self, redis_frontier: RedisURLFrontier):
        """pending_count() derives from get_status_counts() -- confirm the
        exception actually propagates through, not just the method it wraps."""
        redis_frontier.add_url("https://example.com/a", priority=10)

        with patch.object(redis_frontier.redis_conn, "pipeline", side_effect=redis.ConnectionError("simulated outage")):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.pending_count()

    def test_has_pending_still_works_normally_once_redis_is_healthy(self, redis_frontier: RedisURLFrontier):
        """Sanity check: the exception path doesn't change ordinary behavior."""
        assert redis_frontier.has_pending() is False
        redis_frontier.add_url("https://example.com/a", priority=10)
        assert redis_frontier.has_pending() is True


class TestGetNextUrlNeverFakesIdle:
    """get_next_url must raise, not return None indistinguishably from 'no
    eligible domain right now', when Redis is unreachable."""

    def test_get_next_url_raises_instead_of_returning_none(self, redis_frontier: RedisURLFrontier):
        redis_frontier.add_url("https://example.com/a", priority=10)

        with patch.object(redis_frontier, "_claim_next_script", side_effect=redis.ConnectionError("simulated outage")):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.get_next_url()

    def test_get_next_url_still_works_normally_once_redis_is_healthy(self, redis_frontier: RedisURLFrontier):
        redis_frontier.add_url("https://example.com/a", priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None
        assert claim.url == "https://example.com/a"


class TestAddUrlNeverSilentlyLosesUrls:
    """Test 5: a Redis failure during add_url must not be treated as
    'URL successfully handled' (silently dropped, no retry, no signal)."""

    def test_add_url_raises_instead_of_returning_false(self, redis_frontier: RedisURLFrontier):
        with patch.object(redis_frontier, "_add_url_script", side_effect=redis.ConnectionError("simulated outage")):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.add_url("https://example.com/lost", priority=10)

    def test_failed_add_was_not_silently_recorded_as_known(self, redis_frontier: RedisURLFrontier):
        """If the failed add had partially applied (e.g. SADD to urls:known
        before the error), a retried add would wrongly report 'duplicate'.
        It must not: the Lua script never got to execute, so the URL is
        still genuinely new once Redis recovers."""
        with patch.object(redis_frontier, "_add_url_script", side_effect=redis.ConnectionError("simulated outage")):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.add_url("https://example.com/lost", priority=10)

        assert redis_frontier.add_url("https://example.com/lost", priority=10) is True


class TestCompletionFailuresAreVisible:
    """Test 6: mark_visited/mark_failed/mark_skipped failures must be
    visible to the caller and must not silently produce false crawler
    state (e.g. a URL counted as visited that Redis never actually marked)."""

    @pytest.mark.parametrize(
        "method_name,method_args",
        [
            ("mark_visited", ()),
            ("mark_failed", ("boom",)),
            ("mark_skipped", ()),
        ],
    )
    def test_completion_raises_instead_of_silently_swallowing(
        self, redis_frontier: RedisURLFrontier, method_name: str, method_args: tuple
    ):
        redis_frontier.add_url("https://example.com/complete-me", priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        method = getattr(redis_frontier, method_name)
        with patch.object(
            redis_frontier, "_complete_claim_script", side_effect=redis.ConnectionError("simulated outage")
        ):
            with pytest.raises(FrontierUnavailable):
                method(claim, *method_args)

        # The claim must still show as inflight -- Redis never actually
        # recorded the completion, so the crawler's view of state must
        # match that, not "resolved".
        counts = redis_frontier.get_status_counts()
        assert counts["inflight"] == 1
        assert counts["visited"] == 0
        assert counts["failed_permanent"] == 0
        assert counts["skipped"] == 0

        # Once Redis recovers, completing the same (still-current) claim
        # for real must work normally.
        method(claim, *method_args)


class TestRenewClaimDistinguishesOutageFromLostClaim:
    """renew_claim's `None` return must mean 'genuinely stale claim', never
    'Redis is unreachable' -- conflating the two is exactly what caused
    run_with_heartbeat to misreport a live worker's claim as lost during a
    transient Redis blip (docs/architecture/frontier-redis-failure-semantics.md)."""

    def test_renew_claim_raises_on_redis_error_rather_than_returning_none(
        self, redis_frontier: RedisURLFrontier
    ):
        redis_frontier.add_url("https://example.com/renew-me", priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        with patch.object(
            redis_frontier, "_renew_claim_script", side_effect=redis.ConnectionError("simulated outage")
        ):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.renew_claim(claim)

    def test_renew_claim_still_returns_none_for_a_genuinely_stale_claim(
        self, redis_frontier: RedisURLFrontier
    ):
        redis_frontier.add_url("https://example.com/stale", priority=10)
        claim = redis_frontier.get_next_url()
        assert claim is not None

        redis_frontier.mark_visited(claim)  # invalidates the claim's token

        # No Redis failure injected here -- this must stay a clean `None`,
        # not an exception, since Redis answered the question correctly.
        assert redis_frontier.renew_claim(claim) is None


class TestReclaimAndPromoteVisibility:
    def test_reclaim_and_promote_raises_instead_of_returning_zero_zero(
        self, redis_frontier: RedisURLFrontier
    ):
        with patch.object(
            redis_frontier, "_reclaim_and_promote_script", side_effect=redis.ConnectionError("simulated outage")
        ):
            with pytest.raises(FrontierUnavailable):
                redis_frontier.reclaim_and_promote()


class TestSchedulerNeverTreatsOutageAsIdle:
    """Test 3 & 4: the crawler scheduler must never conclude "no more work"
    because of a Redis failure, and must resume normally once Redis
    recovers."""

    @pytest.mark.asyncio
    async def test_scheduler_does_not_shut_down_while_frontier_is_unavailable(
        self, redis_frontier: RedisURLFrontier
    ):
        redis_frontier.add_url("https://example.com/still-pending", priority=10)
        crawler = AsyncCrawler(frontier=redis_frontier, concurrency=1)

        with patch.object(
            redis_frontier, "_claim_next_script", side_effect=redis.ConnectionError("simulated outage")
        ), patch.object(
            redis_frontier.redis_conn, "pipeline", side_effect=redis.ConnectionError("simulated outage")
        ):
            task = asyncio.create_task(crawler.scheduler())
            try:
                # idle_loops needs 10 consecutive successful idle polls
                # (~5s) to shut down; wait well past that under outage.
                await asyncio.sleep(2.5)
                assert crawler._stop_event.is_set() is False, (
                    "scheduler must not conclude the crawl is finished while "
                    "the frontier is unreachable"
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_scheduler_resumes_claiming_once_frontier_recovers(
        self, redis_frontier: RedisURLFrontier
    ):
        redis_frontier.add_url("https://example.com/recoverable", priority=10)
        crawler = AsyncCrawler(frontier=redis_frontier, concurrency=1)

        task = asyncio.create_task(crawler.scheduler())
        try:
            with patch.object(
                redis_frontier, "_claim_next_script", side_effect=redis.ConnectionError("simulated outage")
            ):
                await asyncio.sleep(1.2)
                assert crawler._stop_event.is_set() is False
                assert crawler.queue.empty()

            # Patch lifted -- Redis "recovers". The scheduler's next poll
            # (every 0.5s) should claim the still-pending URL.
            await asyncio.sleep(1.2)
            assert crawler.queue.qsize() == 1
            assert crawler._stop_event.is_set() is False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_scheduler_still_shuts_down_on_a_genuinely_empty_frontier(
        self, redis_frontier: RedisURLFrontier
    ):
        """Sanity check: the fix must not make the scheduler unable to stop
        at all -- a real empty frontier (no injected failure) must still
        trigger shutdown."""
        crawler = AsyncCrawler(frontier=redis_frontier, concurrency=1)

        task = asyncio.create_task(crawler.scheduler())
        try:
            await asyncio.wait_for(task, timeout=10.0)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert crawler._stop_event.is_set() is True
