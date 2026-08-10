"""Tests for the startup-seeding failure policy (Step 7 follow-up review).

See docs/architecture/frontier-redis-failure-semantics.md, "Startup seeding
failure semantics" for the full writeup. `CrawlerManager.load_seed_urls`,
`load_unfinished_urls`, and `load_search_query_urls` all route through
`_make_seed_url_adder`: a Redis outage during startup seeding must never
silently drop a URL (it is durably recorded in `url_database` for retry via
`--unfinished`), must stay distinguishable from an ordinary duplicate/
blacklist skip, and a sustained outage must not multiply its cost by the
size of the seed list (circuit breaker).

Uses Redis DB 2 (reserved for Step 7 failure-injection tests -- never DB 0)
with namespaces dedicated to this file, distinct from
tests/frontier_redis_failure_semantics_test.py's namespace.

Run this test ONLY if Redis is available locally on port 6379.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import redis

from core.config import Config, CrawlerConfig, FrontierConfig, SearchConfig, StorageConfig
from core.crawler_manager import (
    _SEED_ADD_CIRCUIT_BREAKER_THRESHOLD,
    _SEED_ADD_MAX_ATTEMPTS,
    CrawlerManager,
)
from core.redis_frontier import RedisURLFrontier
from utils.url_utils import URLUtils


def _make_manager(tmp_path, namespace: str, seed_urls: list[str] | None = None) -> CrawlerManager:
    sqlite_path = tmp_path / "crawl.db"
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("\n".join(seed_urls or []) + "\n", encoding="utf-8")

    config = Config(
        crawler=CrawlerConfig(
            storage=StorageConfig(sqlite_path=str(sqlite_path)),
            seed_files=[],
            max_pages=1,
            rate_limit=0,
            frontier=FrontierConfig(
                type="redis",
                redis_host="localhost",
                redis_port=6379,
                redis_db=2,
                redis_namespace=namespace,
                max_retries=3,
                base_backoff=0,
                max_backoff=1,
                lease_ttl=90.0,
            ),
        ),
        search=SearchConfig(enabled_engines=[]),
    )
    return CrawlerManager(
        config=config,
        extra_seed_files=[str(seed_file)],
        crawl_engine="http",
        include_seed_files=True,
    )


def _skip_if_redis_unavailable(manager: CrawlerManager) -> None:
    if not isinstance(manager.frontier, RedisURLFrontier):
        pytest.skip("Redis not available on localhost:6379")


def _cleanup(manager: CrawlerManager) -> None:
    if isinstance(manager.frontier, RedisURLFrontier):
        manager.frontier.clear()
    manager.url_database.close()
    manager.domain_database.close()
    if manager.media_database:
        manager.media_database.close()


# All outage scenarios patch the retry delay down so the suite stays fast --
# behavior under test is the retry/circuit-breaker *logic*, not real timing.
_FAST_RETRY = patch("core.crawler_manager._SEED_ADD_RETRY_DELAY_SECONDS", 0.01)


class TestTransientOutageDuringSeeding:
    def test_transient_failure_is_absorbed_by_retry_within_the_same_run(self, tmp_path):
        url = "https://transient.example.com/page"
        manager = _make_manager(tmp_path, "seed_transient", [url])
        try:
            _skip_if_redis_unavailable(manager)

            original = manager.frontier._add_url_script
            call_count = {"n": 0}

            def flaky(*args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    raise redis.ConnectionError("simulated blip")
                return original(*args, **kwargs)

            with _FAST_RETRY, patch.object(manager.frontier, "_add_url_script", side_effect=flaky):
                manager.load_seed_urls()

            assert call_count["n"] == 3, "expected 2 failed attempts + 1 successful attempt"

            # Not deferred -- the retry absorbed the blip within this run.
            assert manager.frontier.has_pending() is True
            # Redis mode no longer mirrors successful adds into url_database
            # (docs/architecture/redis-sqlite-boundary-decision.md) -- only
            # the deferred-on-outage branch of _make_seed_url_adder writes
            # to it, and this URL was never deferred.
            assert manager.url_database.get_urls_and_statuses(["queued", "pending"]) == []

            # And the URL is genuinely claimable from Redis, not just present
            # in local storage.
            claim = manager.frontier.get_next_url()
            assert claim is not None
            assert claim.url == url
        finally:
            _cleanup(manager)


class TestSustainedOutageDefersAndResumeRecovers:
    def test_sustained_outage_defers_url_to_storage_and_resume_recovers_it(self, tmp_path):
        url = "https://sustained.example.com/page"
        manager = _make_manager(tmp_path, "seed_sustained", [url])
        resume_manager = None
        try:
            _skip_if_redis_unavailable(manager)
            config = manager.config

            with _FAST_RETRY, patch.object(
                manager.frontier, "_add_url_script", side_effect=redis.ConnectionError("simulated outage")
            ):
                manager.load_seed_urls()

            # Never entered Redis...
            assert manager.frontier.has_pending() is False
            # ...but durably recorded, not silently lost.
            assert manager.url_database.get_urls_and_statuses(["queued", "pending"]) == [(url, "queued")]

            _cleanup(manager)
            manager = None  # already cleaned up -- don't clean up twice

            # Simulate the next run, with Redis healthy again, using --unfinished.
            resume_manager = CrawlerManager(
                config=config,
                crawl_engine="http",
                resume_unfinished=True,
                include_seed_files=False,
            )
            resume_manager.load_unfinished_urls()

            assert resume_manager.frontier.has_pending() is True
            claim = resume_manager.frontier.get_next_url()
            assert claim is not None
            assert claim.url == url
        finally:
            if manager is not None:
                _cleanup(manager)
            if resume_manager is not None:
                _cleanup(resume_manager)

    def test_unfinished_loader_itself_also_defers_rather_than_loses_urls_on_outage(self, tmp_path):
        """The resume loader (`--unfinished`) routes through the same
        adder -- an outage *during* a resume attempt must not lose the URL
        either, or a crawl could never recover from a Redis outage that
        happens to coincide with every resume attempt."""
        url = "https://resume-outage.example.com/page"
        manager = _make_manager(tmp_path, "seed_resume_outage", [])
        try:
            _skip_if_redis_unavailable(manager)
            manager.url_database.add_url(url, status="queued")

            with _FAST_RETRY, patch.object(
                manager.frontier, "_add_url_script", side_effect=redis.ConnectionError("simulated outage")
            ):
                manager.load_unfinished_urls()

            assert manager.frontier.has_pending() is False
            recorded = {u for u, _ in manager.url_database.get_urls_and_statuses(["queued", "pending"])}
            assert url in recorded
        finally:
            _cleanup(manager)


class TestOrdinarySeedingUnaffected:
    def test_duplicate_url_is_still_rejected_not_deferred(self, tmp_path):
        url = "https://dup.example.com/page"
        manager = _make_manager(tmp_path, "seed_dup", [url, url])
        try:
            _skip_if_redis_unavailable(manager)

            with patch.object(
                manager.frontier, "_add_url_script", wraps=manager.frontier._add_url_script
            ) as spy:
                manager.load_seed_urls()

            # Both lines attempted Redis (no retry short-circuit for an
            # ordinary duplicate -- it's not a FrontierUnavailable at all).
            assert spy.call_count == 2

            counts = manager.frontier.get_status_counts()
            assert counts["queued"] == 1

            # Neither the accepted nor the rejected-duplicate outcome is a
            # deferral, so url_database is never touched (Redis mode no
            # longer mirrors successful/rejected adds into it).
            recorded = {u for u, _ in manager.url_database.get_urls_and_statuses(["queued", "pending"])}
            assert recorded == set()
        finally:
            _cleanup(manager)

    def test_blacklisted_url_is_still_rejected_without_touching_redis(self, tmp_path):
        blacklist_path = tmp_path / "domain_blacklist.txt"
        blacklist_path.write_text("blacklisted-seed.example.com\n", encoding="utf-8")
        original_blacklist_path = URLUtils._blacklist_path
        original_enabled = URLUtils._blacklist_enabled

        url = "https://blacklisted-seed.example.com/page"
        manager = _make_manager(tmp_path, "seed_blacklist", [url])
        try:
            _skip_if_redis_unavailable(manager)
            URLUtils.set_blacklist_path(str(blacklist_path))
            URLUtils.set_blacklist_enabled(True)

            with patch.object(manager.frontier, "_add_url_script") as spy:
                manager.load_seed_urls()
            spy.assert_not_called()  # blacklist filtering happens before any Redis call

            counts = manager.frontier.get_status_counts()
            assert counts["queued"] == 0
            assert manager.url_database.get_urls_and_statuses(["queued", "pending"]) == []
        finally:
            URLUtils.set_blacklist_path(str(original_blacklist_path))
            URLUtils.set_blacklist_enabled(original_enabled)
            _cleanup(manager)

    def test_healthy_seeding_uses_exactly_one_call_per_url_no_wasted_retries(self, tmp_path):
        urls = [f"https://healthy{i}.example.com/page" for i in range(5)]
        manager = _make_manager(tmp_path, "seed_healthy", urls)
        try:
            _skip_if_redis_unavailable(manager)

            with patch.object(
                manager.frontier, "_add_url_script", wraps=manager.frontier._add_url_script
            ) as spy:
                manager.load_seed_urls()

            assert spy.call_count == len(urls)
            counts = manager.frontier.get_status_counts()
            assert counts["queued"] == len(urls)
            # None of these were deferred, so url_database stays empty --
            # Redis is the sole source of truth for a healthy seeding run.
            recorded = {u for u, _ in manager.url_database.get_urls_and_statuses(["queued", "pending"])}
            assert recorded == set()
        finally:
            _cleanup(manager)


class TestSustainedOutageCircuitBreaker:
    def test_circuit_breaker_stops_wasting_retries_but_still_defers_every_url(self, tmp_path):
        urls = [f"https://circuit{i}.example.com/page" for i in range(6)]
        manager = _make_manager(tmp_path, "seed_circuit", urls)
        try:
            _skip_if_redis_unavailable(manager)

            with _FAST_RETRY, patch.object(
                manager.frontier, "_add_url_script", side_effect=redis.ConnectionError("simulated outage")
            ) as spy:
                manager.load_seed_urls()

            # Exactly `_SEED_ADD_CIRCUIT_BREAKER_THRESHOLD` URLs pay the full
            # per-URL retry budget before the breaker trips; the remaining
            # URLs are deferred immediately with zero further Redis calls --
            # proving a sustained outage does not scale retry cost with the
            # size of the seed list (no uncontrolled startup loop).
            assert spy.call_count == _SEED_ADD_CIRCUIT_BREAKER_THRESHOLD * _SEED_ADD_MAX_ATTEMPTS

            # No URL silently lost -- every one of the 6 is durably recorded.
            assert manager.frontier.has_pending() is False
            recorded = {u for u, _ in manager.url_database.get_urls_and_statuses(["queued", "pending"])}
            assert recorded == set(urls)
        finally:
            _cleanup(manager)


class TestSeedUrlAdderDirectly:
    """Direct unit coverage of the shared per-URL adder (also exercised by
    `load_search_query_urls`, which is harder to drive end-to-end here
    since it depends on live search-engine discovery)."""

    def test_adder_returns_accepted_rejected_deferred_correctly(self, tmp_path):
        manager = _make_manager(tmp_path, "seed_adder_direct", [])
        try:
            _skip_if_redis_unavailable(manager)
            add_url = manager._make_seed_url_adder()

            assert add_url("https://direct-a.example.com/x", 10, "") == "accepted"
            assert add_url("https://direct-a.example.com/x", 10, "") == "rejected"  # duplicate

            with _FAST_RETRY, patch.object(
                manager.frontier, "_add_url_script", side_effect=redis.ConnectionError("simulated outage")
            ):
                assert add_url("https://direct-b.example.com/y", 10, "") == "deferred"

            recorded = {u for u, _ in manager.url_database.get_urls_and_statuses(["queued", "pending"])}
            assert "https://direct-b.example.com/y" in recorded
        finally:
            _cleanup(manager)
