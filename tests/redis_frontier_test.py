"""Tests for the Redis v2 frontier (core/redis_frontier.py).

Covers dedup, atomic claiming, domain-priority scheduling, rate limiting,
and the claim/lease/retry machinery from docs/architecture/frontier-adr.md
(FrontierClaim tokens, stale-claim rejection, reclaim_and_promote).

Run this test ONLY if Redis is available locally on port 6379.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
import redis

from core.redis_frontier import RedisURLFrontier
from storage.url_database import URLDatabase


@pytest.fixture
def redis_frontier() -> RedisURLFrontier:
    """Create a Redis frontier and clear it for testing."""
    try:
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,  # Use DB 1 for testing to avoid production data
            namespace="test_crawler",
            rate_limit=0,  # Disable rate limiting for tests
        )
        frontier.clear()  # Clean slate for test
        yield frontier
        frontier.clear()  # Clean up after test
        frontier.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


@pytest.fixture
def url_database() -> URLDatabase:
    """Create a test URL database in memory (SQLite)."""
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    db = URLDatabase(path=path)
    yield db
    db.close()

    try:
        os.remove(path)
    except OSError:
        pass


def _force_expire_lease(frontier: RedisURLFrontier, url: str) -> None:
    """Test hook: backdate a URL's inflight lease score into the past,
    simulating an abandoned/crashed worker without waiting out lease_ttl."""
    frontier.redis_conn.zadd(frontier._key("inflight"), {url: 0})


class TestMultiWorkerCoordination:
    """Test multi-worker coordination through Redis frontier."""

    def test_add_url_deduplication(self, redis_frontier: RedisURLFrontier):
        """Verify that adding same URL twice doesn't duplicate."""
        url = "https://piracy.example.com/movie1"

        result1 = redis_frontier.add_url(url, priority=10)
        result2 = redis_frontier.add_url(url, priority=10)

        assert result1 is True, "First add should succeed"
        assert result2 is False, "Duplicate add should fail"

        count = redis_frontier.pending_count()
        assert count == 1, f"Should have exactly 1 URL, got {count}"

    def test_concurrent_worker_adds(self, redis_frontier: RedisURLFrontier):
        """Simulate multiple workers adding URLs concurrently."""
        urls = [f"https://piracy.example.com/movie{i}" for i in range(100)]

        def add_urls_worker(start_idx: int, batch_size: int):
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace="test_crawler",
            )
            added = 0
            for i in range(start_idx, start_idx + batch_size):
                if frontier.add_url(urls[i], priority=10):
                    added += 1
            frontier.close()
            return added

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(add_urls_worker, i * 25, 25) for i in range(4)]
            results = [f.result() for f in futures]

        total_added = sum(results)
        assert total_added == 100, f"Should add 100 URLs, got {total_added}"

        pending = redis_frontier.pending_count()
        assert pending == 100, f"Should have 100 pending, got {pending}"

    def test_get_next_url_no_duplicates(self, redis_frontier: RedisURLFrontier):
        """Verify that get_next_url doesn't return duplicate claims to different workers."""
        urls = [f"https://piracy.example.com/page{i}" for i in range(10)]

        for url in urls:
            redis_frontier.add_url(url, priority=10)

        def worker_fetch():
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace="test_crawler",
                rate_limit=0,
            )
            claimed = []
            for _ in range(3):
                claim = frontier.get_next_url()
                if claim:
                    claimed.append(claim)
            frontier.close()
            return claimed

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(worker_fetch) for _ in range(3)]
            results = [f.result() for f in futures]

        fetched_claims = [claim for worker_claims in results for claim in worker_claims]
        fetched_urls = [claim.url for claim in fetched_claims]
        fetched_tokens = [claim.token for claim in fetched_claims]

        assert len(fetched_urls) == len(set(fetched_urls)), (
            f"Found duplicate URLs across workers: {fetched_urls}"
        )
        assert len(fetched_tokens) == len(set(fetched_tokens)), "Every claim token must be unique"
        assert len(fetched_urls) == 9, f"Should fetch 9 URLs (3 workers x 3 each), got {len(fetched_urls)}"

    def test_concurrent_claims_same_domain_never_duplicate(self, redis_frontier: RedisURLFrontier):
        """Stress the atomic pop under contention: many threads racing to claim
        URLs from a single domain must never receive the same URL/token twice."""
        n_urls = 200
        urls = [f"https://contested.example.com/page{i}" for i in range(n_urls)]
        for url in urls:
            redis_frontier.add_url(url, priority=10)

        def worker_claim_all():
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace="test_crawler",
                rate_limit=0,
            )
            claims = []
            while True:
                claim = frontier.get_next_url()
                if claim is None:
                    break
                claims.append(claim)
            frontier.close()
            return claims

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker_claim_all) for _ in range(8)]
            results = [f.result() for f in futures]

        all_claims = [c for worker_claims in results for c in worker_claims]
        all_urls = [c.url for c in all_claims]
        all_tokens = [c.token for c in all_claims]

        assert len(all_urls) == n_urls, f"Expected {n_urls} total claims, got {len(all_urls)}"
        assert len(set(all_urls)) == n_urls, "Two workers claimed the same URL"
        assert len(set(all_tokens)) == n_urls, "Two claims shared the same token"

    def test_mark_visited_consistency(self, redis_frontier: RedisURLFrontier):
        """Verify mark_visited is consistent across workers."""
        url = "https://piracy.example.com/film"

        redis_frontier.add_url(url, priority=10)

        claim = redis_frontier.get_next_url()
        assert claim.url == url, "Should fetch the added URL"

        frontier2 = RedisURLFrontier(
            redis_host="localhost", redis_port=6379, redis_db=1, namespace="test_crawler"
        )
        claim2 = frontier2.get_next_url()
        frontier2.close()
        assert claim2 is None, "URL is already claimed, worker 2 should get nothing"

        redis_frontier.mark_visited(claim)

        frontier3 = RedisURLFrontier(
            redis_host="localhost", redis_port=6379, redis_db=1, namespace="test_crawler"
        )
        claim3 = frontier3.get_next_url()
        frontier3.close()

        assert claim3 is None, "URL should not be available after marking visited"

        counts = redis_frontier.get_status_counts()
        assert counts.get("visited", 0) == 1, "Should have 1 visited URL"
        assert counts.get("inflight", 0) == 0

    def test_rate_limit_per_domain(self, redis_frontier: RedisURLFrontier):
        """Verify that rate limiting is enforced per domain."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_ratelimit",
            rate_limit=1.0,
        )
        frontier.clear()

        domain = "https://piracy.example.com"
        urls = [f"{domain}/page{i}" for i in range(3)]

        for url in urls:
            frontier.add_url(url, priority=10)

        claim1 = frontier.get_next_url()
        assert claim1.url == urls[0], "Should get first URL"

        claim2 = frontier.get_next_url()
        assert claim2 is None, "Should not get another URL from same domain immediately"

        time.sleep(1.1)

        claim3 = frontier.get_next_url()
        assert claim3.url == urls[1], "Should get next URL from same domain after rate limit"

        frontier.clear()
        frontier.close()

    def test_rate_limited_domain_does_not_block_lower_priority_eligible_domain(
        self, redis_frontier: RedisURLFrontier
    ):
        """A rate-gated top-priority domain must not block an eligible domain
        further down domain_heads from yielding work."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_rategate",
            rate_limit=60.0,
        )
        frontier.clear()
        try:
            frontier.add_url("https://hot.example.com/a", priority=1)
            claim = frontier.get_next_url()
            assert claim.domain == "hot.example.com"

            # hot.example.com is now rate-gated for 60s. A lower-priority
            # (higher priority number) but currently-eligible domain must
            # still be claimable, not blocked behind the gated domain.
            frontier.add_url("https://hot.example.com/b", priority=1)
            frontier.add_url("https://cold.example.com/a", priority=50)

            next_claim = frontier.get_next_url()
            assert next_claim is not None, "Rate-gated top domain must not block other domains"
            assert next_claim.domain == "cold.example.com"
        finally:
            frontier.clear()
            frontier.close()

    def test_priority_ordering_across_domains_via_domain_heads(self, redis_frontier: RedisURLFrontier):
        """Global priority must win over insertion order / domain identity:
        a low-priority-number URL added later on a fresh domain must still be
        claimed before a high-priority-number URL on a domain added earlier."""
        redis_frontier.add_url("https://early.example.com/a", priority=50)
        redis_frontier.add_url("https://mid.example.com/a", priority=25)
        redis_frontier.add_url("https://late.example.com/a", priority=1)

        first = redis_frontier.get_next_url()
        second = redis_frontier.get_next_url()
        third = redis_frontier.get_next_url()

        assert [first.domain, second.domain, third.domain] == [
            "late.example.com",
            "mid.example.com",
            "early.example.com",
        ]

    def test_clear_frontier(self, redis_frontier: RedisURLFrontier):
        """Verify clear() wipes all state."""
        for i in range(5):
            redis_frontier.add_url(f"https://example.com/page{i}", priority=10)

        claim = redis_frontier.get_next_url()
        redis_frontier.mark_visited(claim)

        counts = redis_frontier.get_status_counts()
        assert counts["queued"] > 0, "Should have queued URLs"

        redis_frontier.clear()

        counts = redis_frontier.get_status_counts()
        assert counts.get("queued", 0) == 0, "Should have no queued after clear"
        assert counts.get("visited", 0) == 0, "Should have no visited after clear"

    def test_namespace_isolation(self):
        """Verify that different namespaces don't interfere."""
        frontier1 = RedisURLFrontier(
            redis_host="localhost", redis_port=6379, redis_db=1, namespace="crawler_a"
        )
        frontier2 = RedisURLFrontier(
            redis_host="localhost", redis_port=6379, redis_db=1, namespace="crawler_b"
        )

        try:
            frontier1.clear()
            frontier2.clear()

            frontier1.add_url("https://example.com/a", priority=10)

            claim = frontier2.get_next_url()
            assert claim is None, "Different namespace should not see other's URLs"

            frontier2.add_url("https://example.com/b", priority=10)

            claim = frontier1.get_next_url()
            assert claim.url == "https://example.com/a", "Should only see own namespace"
        finally:
            frontier1.clear()
            frontier2.clear()
            frontier1.close()
            frontier2.close()


class TestClaimLifecycle:
    """Claim tokens, stale-claim rejection, renewal, retry/backoff, and
    lease-expiry reclaim -- docs/architecture/frontier-adr.md §3-4, §12."""

    def test_stale_claim_rejected_after_lease_reclaim(self, redis_frontier: RedisURLFrontier):
        """A stale (since-reclaimed) worker must never be able to complete a
        newer claim on the same URL -- the core concurrency guarantee."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_stale",
            rate_limit=0,
            max_retries=3,
            base_backoff=0,
        )
        frontier.clear()
        try:
            url = "https://stale.example.com/page"
            frontier.add_url(url, priority=10)

            old_claim = frontier.get_next_url()
            assert old_claim is not None

            _force_expire_lease(frontier, url)
            reclaimed, requeued = frontier.reclaim_and_promote()
            assert reclaimed == 1
            assert requeued == 1, "base_backoff=0 means it should be immediately re-queued"

            new_claim = frontier.get_next_url()
            assert new_claim is not None
            assert new_claim.token != old_claim.token
            assert new_claim.attempt == old_claim.attempt + 1

            # The old worker finally wakes up and tries to complete its stale claim.
            frontier.mark_visited(old_claim)
            counts = frontier.get_status_counts()
            assert counts["visited"] == 0, "Stale completion must be a no-op"
            assert counts["inflight"] == 1, "New claim must remain untouched"

            # The current owner completes successfully.
            frontier.mark_visited(new_claim)
            counts = frontier.get_status_counts()
            assert counts["visited"] == 1
            assert counts["inflight"] == 0
        finally:
            frontier.clear()
            frontier.close()

    def test_renew_claim_extends_lease_and_fails_for_reclaimed_claim(
        self, redis_frontier: RedisURLFrontier
    ):
        url = "https://renew.example.com/page"
        redis_frontier.add_url(url, priority=10)

        claim = redis_frontier.get_next_url()
        assert claim is not None

        renewed = redis_frontier.renew_claim(claim)
        assert renewed is not None
        assert renewed.lease_expires_at >= claim.lease_expires_at
        assert renewed.token == claim.token

        _force_expire_lease(redis_frontier, url)
        redis_frontier.reclaim_and_promote()

        stale_renewal = redis_frontier.renew_claim(claim)
        assert stale_renewal is None, "Renewal must fail once the claim has been reclaimed"

    def test_mark_failed_retries_with_growing_backoff_then_fails_permanently(
        self, redis_frontier: RedisURLFrontier
    ):
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_retry",
            rate_limit=0,
            max_retries=3,
            base_backoff=0,
            max_backoff=60,
        )
        frontier.clear()
        try:
            url = "https://retry.example.com/page"
            frontier.add_url(url, priority=10)

            attempts_seen = []
            claim = frontier.get_next_url()
            while claim is not None and len(attempts_seen) < 10:
                attempts_seen.append(claim.attempt)
                frontier.mark_failed(claim, "boom")
                frontier.reclaim_and_promote()  # promote due (backoff=0) retry back to queue
                claim = frontier.get_next_url()

            assert attempts_seen == [1, 2, 3], f"Expected 3 increasing attempts, got {attempts_seen}"

            counts = frontier.get_status_counts()
            assert counts["failed_permanent"] == 1
            assert counts["retry_scheduled"] == 0
            assert counts["queued"] == 0
            assert counts["inflight"] == 0
        finally:
            frontier.clear()
            frontier.close()

    def test_crash_injection_reclaim_requeues_below_max_retries(self, redis_frontier: RedisURLFrontier):
        """Claim a URL, never complete it, force-expire the lease, run one
        recovery sweep -- it must become claimable again."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_crash",
            rate_limit=0,
            max_retries=3,
            base_backoff=0,
        )
        frontier.clear()
        try:
            url = "https://crash.example.com/page"
            frontier.add_url(url, priority=10)

            claim = frontier.get_next_url()
            assert claim is not None
            # Simulate a crashed worker: never mark_visited/failed/skipped.

            _force_expire_lease(frontier, url)
            reclaimed, requeued = frontier.reclaim_and_promote()
            assert reclaimed == 1
            assert requeued == 1

            new_claim = frontier.get_next_url()
            assert new_claim is not None
            assert new_claim.url == url
            assert new_claim.attempt == 2
        finally:
            frontier.clear()
            frontier.close()

    def test_crash_injection_reclaim_terminalizes_at_max_retries(self, redis_frontier: RedisURLFrontier):
        """A crashed claim that has already exhausted its retries must land
        in failed_permanent on reclaim, not be requeued forever."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_crash_exhausted",
            rate_limit=0,
            max_retries=1,
            base_backoff=0,
        )
        frontier.clear()
        try:
            url = "https://crash-exhausted.example.com/page"
            frontier.add_url(url, priority=10)

            claim = frontier.get_next_url()
            assert claim is not None
            assert claim.attempt == 1

            _force_expire_lease(frontier, url)
            reclaimed, requeued = frontier.reclaim_and_promote()
            assert reclaimed == 1
            assert requeued == 0

            assert frontier.get_next_url() is None
            counts = frontier.get_status_counts()
            assert counts["failed_permanent"] == 1
            assert counts["inflight"] == 0
            assert counts["retry_scheduled"] == 0
        finally:
            frontier.clear()
            frontier.close()

    def test_reclaim_and_promote_is_constant_round_trips_regardless_of_domain_count(
        self, redis_frontier: RedisURLFrontier
    ):
        """Guards against regressing back to SCAN-style O(domains) behavior:
        reclaim_and_promote must issue the same number of client->Redis
        commands whether 3 domains or 60 domains are involved."""

        def run_with_n_domains(n_domains: int) -> int:
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace=f"test_crawler_rtcount_{n_domains}",
                rate_limit=0,
                max_retries=3,
                base_backoff=0,
            )
            frontier.clear()
            try:
                urls = [f"https://domain{i}.example.com/page" for i in range(n_domains)]
                for url in urls:
                    frontier.add_url(url, priority=10)
                    claim = frontier.get_next_url()
                    _force_expire_lease(frontier, claim.url)

                original = frontier.redis_conn.execute_command
                call_count = {"n": 0}

                def counting_execute_command(*args, **kwargs):
                    call_count["n"] += 1
                    return original(*args, **kwargs)

                with patch.object(
                    frontier.redis_conn, "execute_command", side_effect=counting_execute_command
                ):
                    frontier.reclaim_and_promote(batch_size=1000)

                return call_count["n"]
            finally:
                frontier.clear()
                frontier.close()

        calls_small = run_with_n_domains(3)
        calls_large = run_with_n_domains(60)

        assert calls_small == calls_large, (
            f"reclaim_and_promote round trips scaled with domain count: "
            f"{calls_small} (3 domains) vs {calls_large} (60 domains)"
        )
        assert calls_small <= 2, f"Expected a single script invocation, got {calls_small} calls"


class TestMarkDeferred:
    """`mark_deferred` (N3, docs/architecture/network-failure-handling-design.md
    §6) -- attempt-count neutrality, CAS safety under a stale/superseded
    claim, terminal-set exclusion, and the fixed (non-exponential) requeue
    delay."""

    def test_claim_then_mark_deferred_leaves_attempt_budget_net_zero(
        self, redis_frontier: RedisURLFrontier
    ):
        """claim -> mark_deferred must undo exactly the increment claim_next
        made, and never route to failed_permanent -- repeated indefinitely
        without ever consuming retry budget."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_deferred",
            rate_limit=0,
            max_retries=1,
            base_backoff=0,
            deferred_requeue_delay_seconds=0,
        )
        frontier.clear()
        try:
            url = "https://outage.example.com/page"
            frontier.add_url(url, priority=10)

            for i in range(5):
                claim = frontier.get_next_url()
                assert claim is not None
                assert claim.attempt == 1  # never climbs: the increment is always undone
                frontier.mark_deferred(claim, reason="confirmed local outage")
                if i < 4:
                    # retry_scheduled -> domain queue promotion is
                    # reclaim_and_promote's job (matches production: the
                    # recovery loop, not get_next_url, promotes due
                    # retries for the Redis backend). Skip on the last
                    # iteration so the final state below still has one
                    # entry sitting in retry_scheduled, unpromoted.
                    frontier.reclaim_and_promote()

            counts = frontier.get_status_counts()
            assert counts["failed_permanent"] == 0
            assert counts["retry_scheduled"] == 1
            assert frontier.redis_conn.get(frontier._key("attempts", url)) in ("0", None)
        finally:
            frontier.clear()
            frontier.close()

    def test_mark_deferred_uses_fixed_delay_not_exponential_backoff(
        self, redis_frontier: RedisURLFrontier
    ):
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_deferred_delay",
            rate_limit=0,
            max_retries=5,
            base_backoff=100,
            max_backoff=1000,
            deferred_requeue_delay_seconds=3,
        )
        frontier.clear()
        try:
            url = "https://outage.example.com/page"
            frontier.add_url(url, priority=10)

            claim = frontier.get_next_url()
            before = time.time()
            frontier.mark_deferred(claim, reason="offline")

            scheduled_at = frontier.redis_conn.zscore(frontier._key("retry_scheduled"), url)
            assert scheduled_at is not None
            # Fixed ~3s delay, nowhere near base_backoff (100s) -- proves
            # the exponential target-failure ladder was not used.
            assert scheduled_at < before + 100
            assert scheduled_at >= before + 3 - 1
        finally:
            frontier.clear()
            frontier.close()

    def test_stale_claim_mark_deferred_does_not_corrupt_newer_claim(
        self, redis_frontier: RedisURLFrontier
    ):
        """CRITICAL RACE REQUIREMENT: a stale claim's mark_deferred must not
        decrement an attempt counter belonging to a newer claim, nor
        complete/requeue the newer claim's ownership out from under it."""
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler_deferred_race",
            rate_limit=0,
            max_retries=5,
            base_backoff=0,
        )
        frontier.clear()
        try:
            url = "https://racey.example.com/page"
            frontier.add_url(url, priority=10)

            old_claim = frontier.get_next_url()
            assert old_claim is not None
            assert old_claim.attempt == 1

            # Simulate the old worker's claim being abandoned/reclaimed
            # (e.g. lease-expiry recovery) and a *different* worker
            # claiming the URL again before the old worker's mark_deferred
            # call finally lands.
            _force_expire_lease(frontier, url)
            reclaimed, requeued = frontier.reclaim_and_promote()
            assert reclaimed == 1 and requeued == 1

            new_claim = frontier.get_next_url()
            assert new_claim is not None
            assert new_claim.token != old_claim.token
            assert new_claim.attempt == 2

            # The old (stale) worker's mark_deferred call arrives late.
            frontier.mark_deferred(old_claim, reason="stale/late defer")

            # The newer claim's attempt count and ownership must be
            # completely untouched by the stale call.
            assert frontier.redis_conn.get(frontier._key("attempts", url)) == "2"
            assert frontier.redis_conn.hget(frontier._key("claim", url), "token") == new_claim.token
            assert frontier.redis_conn.zscore(frontier._key("inflight"), url) is not None

            # The current owner can still complete normally afterwards.
            frontier.mark_visited(new_claim)
            counts = frontier.get_status_counts()
            assert counts["visited"] == 1
            assert counts["failed_permanent"] == 0
        finally:
            frontier.clear()
            frontier.close()

    def test_mark_deferred_removes_inflight_and_claim(self, redis_frontier: RedisURLFrontier):
        url = "https://outage.example.com/inflight-check"
        redis_frontier.add_url(url, priority=10)

        claim = redis_frontier.get_next_url()
        assert redis_frontier.redis_conn.zscore(redis_frontier._key("inflight"), url) is not None

        redis_frontier.mark_deferred(claim, reason="offline")

        assert redis_frontier.redis_conn.zscore(redis_frontier._key("inflight"), url) is None
        assert redis_frontier.redis_conn.hget(redis_frontier._key("claim", url), "token") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
