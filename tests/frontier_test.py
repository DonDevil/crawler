"""Tests for URL frontier behavior."""

import time

from core.url_frontier import URLFrontier
from storage.url_database import URLDatabase
from utils.url_utils import URLUtils


def test_frontier_marks_cleaned_urls_as_visited_and_prevents_requeue():
    frontier = URLFrontier(rate_limit=0)
    raw_url = "https://example.com/page?utm_source=newsletter"
    cleaned_url = "https://example.com/page"

    frontier.add_url(raw_url)

    claim = frontier.get_next_url()
    assert claim.url == cleaned_url

    frontier.mark_visited(claim)
    frontier.add_url(cleaned_url)

    assert cleaned_url in frontier.visited
    assert frontier.get_next_url() is None


def test_frontier_keeps_other_domains_available_when_one_is_rate_limited():
    frontier = URLFrontier(rate_limit=60)
    frontier.add_url("https://a.example.com/1")
    frontier.add_url("https://b.example.com/1")

    first = frontier.get_next_url()
    second = frontier.get_next_url()

    assert {first.url, second.url} == {"https://a.example.com/1", "https://b.example.com/1"}


def test_frontier_skips_urls_already_visited_in_database(tmp_path):
    db_path = tmp_path / "crawl.db"
    database = URLDatabase(path=str(db_path))

    try:
        visited_url = "https://example.com/already-visited"
        database.add_url(visited_url, status="visited")

        frontier = URLFrontier(rate_limit=0, url_database=database)
        frontier.add_url(visited_url)

        assert frontier.get_next_url() is None
        assert frontier.pending_count() == 0
    finally:
        database.close()


def test_frontier_drops_queued_urls_when_domain_becomes_blacklisted(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        frontier = URLFrontier(rate_limit=0)
        frontier.add_url("https://blocked.example.com/path")

        blacklist_path.write_text("example.com\n", encoding="utf-8")

        assert frontier.get_next_url() is None
        assert frontier.get_status_counts()["skipped"] == 1
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_mark_failed_retries_with_backoff_then_fails_permanently():
    frontier = URLFrontier(rate_limit=0, max_retries=2, base_backoff=0, max_backoff=0)
    frontier.add_url("https://example.com/flaky")

    first_claim = frontier.get_next_url()
    assert first_claim.attempt == 1

    frontier.mark_failed(first_claim, "transient error")
    assert frontier.get_status_counts()["retry_scheduled"] == 1

    second_claim = frontier.get_next_url()
    assert second_claim is not None
    assert second_claim.url == first_claim.url
    assert second_claim.attempt == 2
    assert second_claim.token != first_claim.token

    frontier.mark_failed(second_claim, "still failing")

    counts = frontier.get_status_counts()
    assert counts["failed_permanent"] == 1
    assert counts["retry_scheduled"] == 0
    assert frontier.get_next_url() is None


def test_stale_claim_is_ignored_by_completion_methods():
    frontier = URLFrontier(rate_limit=0, max_retries=3, base_backoff=0, max_backoff=0)
    frontier.add_url("https://example.com/retry-me")

    first_claim = frontier.get_next_url()
    frontier.mark_failed(first_claim, "transient")

    second_claim = frontier.get_next_url()
    assert second_claim.token != first_claim.token

    frontier.mark_visited(first_claim)
    assert frontier.get_status_counts()["visited"] == 0

    frontier.mark_visited(second_claim)
    assert frontier.get_status_counts()["visited"] == 1

    frontier.mark_visited(second_claim)
    frontier.mark_failed(second_claim, "too late")
    frontier.mark_skipped(second_claim)
    assert frontier.get_status_counts()["visited"] == 1


def test_status_counts_partition_every_known_url():
    frontier = URLFrontier(rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0)
    for domain in ("a", "b", "c", "d"):
        frontier.add_url(f"https://{domain}.example.com/1")

    visited_claim = frontier.get_next_url()
    frontier.mark_visited(visited_claim)

    failed_claim = frontier.get_next_url()
    frontier.mark_failed(failed_claim, "dead")

    inflight_claim = frontier.get_next_url()
    assert inflight_claim is not None

    counts = frontier.get_status_counts()
    assert counts == {
        "queued": 1,
        "inflight": 1,
        "retry_scheduled": 0,
        "visited": 1,
        "skipped": 0,
        "failed_permanent": 1,
    }
    assert sum(counts.values()) == 4


def test_renew_claim_succeeds_for_current_owner_and_fails_after_completion():
    frontier = URLFrontier(rate_limit=0)
    frontier.add_url("https://example.com/renewable")

    claim = frontier.get_next_url()
    renewed = frontier.renew_claim(claim)

    assert renewed is not None
    assert renewed.token == claim.token
    assert renewed.lease_expires_at >= claim.lease_expires_at

    frontier.mark_visited(claim)
    assert frontier.renew_claim(claim) is None


def test_mark_deferred_leaves_retry_budget_unchanged_and_never_fails_permanent():
    """N2 §6: claim -> mark_deferred must have zero net effect on the URL's
    retry budget (attempts += 1 at claim time, -= 1 at defer time), and
    must never route to failed_permanent regardless of how many times it
    happens."""
    frontier = URLFrontier(
        rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0, deferred_requeue_delay_seconds=0
    )
    frontier.add_url("https://example.com/outage")

    for _ in range(5):
        claim = frontier.get_next_url()
        assert claim.attempt == 1  # never climbs past 1: the increment is always undone
        frontier.mark_deferred(claim, reason="confirmed local outage")

    counts = frontier.get_status_counts()
    assert counts["failed_permanent"] == 0
    assert counts["retry_scheduled"] == 1  # requeued, waiting to be reclaimed
    assert frontier.get_next_url() is not None


def test_mark_deferred_uses_fixed_delay_not_exponential_backoff():
    frontier = URLFrontier(rate_limit=0, max_retries=5, base_backoff=100, max_backoff=1000)
    frontier.add_url("https://example.com/outage")

    claim = frontier.get_next_url()
    before = time.time()
    frontier.mark_deferred(claim, reason="offline")

    # The only entry in the retry heap must be scheduled using
    # deferred_requeue_delay_seconds (default 10s), not base_backoff (100s)
    # -- this is not a target-failure retry.
    scheduled_at = frontier._retry_heap[0][0]
    assert scheduled_at < before + 100
    assert scheduled_at >= before + frontier.deferred_requeue_delay_seconds - 1


def test_mark_deferred_ignores_stale_claim_without_corrupting_newer_claim():
    frontier = URLFrontier(rate_limit=0, max_retries=3, base_backoff=0, max_backoff=0)
    frontier.add_url("https://example.com/racey")

    first_claim = frontier.get_next_url()
    frontier.mark_failed(first_claim, "transient")  # attempt now consumed normally

    second_claim = frontier.get_next_url()
    assert second_claim.token != first_claim.token
    assert second_claim.attempt == 2

    # A stale mark_deferred (using the old, already-completed claim) must
    # be a no-op -- it must not touch second_claim's attempt count or
    # complete it out from under the newer owner.
    frontier.mark_deferred(first_claim, reason="late/stale")

    assert frontier._attempts[second_claim.url] == 2
    assert frontier._is_current_claim(second_claim)

    frontier.mark_visited(second_claim)
    assert frontier.get_status_counts()["visited"] == 1
    assert frontier.get_status_counts()["failed_permanent"] == 0
