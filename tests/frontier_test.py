"""Tests for URL frontier behavior."""

from core.url_frontier import URLFrontier


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