"""Tests for URL frontier behavior."""

from core.url_frontier import URLFrontier
from storage.url_database import URLDatabase
from utils.url_utils import URLUtils


def test_frontier_marks_cleaned_urls_as_visited_and_prevents_requeue():
    frontier = URLFrontier(rate_limit=0)
    raw_url = "https://example.com/page?utm_source=newsletter"
    cleaned_url = "https://example.com/page"

    frontier.add_url(raw_url)

    assert frontier.get_next_url() == cleaned_url

    frontier.mark_visited(raw_url)
    frontier.add_url(cleaned_url)

    assert cleaned_url in frontier.visited
    assert frontier.get_next_url() is None


def test_frontier_keeps_other_domains_available_when_one_is_rate_limited():
    frontier = URLFrontier(rate_limit=60)
    frontier.add_url("https://a.example.com/1")
    frontier.add_url("https://b.example.com/1")

    first = frontier.get_next_url()
    second = frontier.get_next_url()

    assert {first, second} == {"https://a.example.com/1", "https://b.example.com/1"}


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
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)