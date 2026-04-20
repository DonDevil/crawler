"""Tests for URL validation and normalization helpers."""

from pathlib import Path

from utils.url_utils import URLUtils


def test_clean_url_rejects_single_label_hosts():
    assert URLUtils.clean_url("http://search/?q=news") is None


def test_clean_url_rejects_markup_artifacts():
    bad_url = "http://www.w3.org/2000/svg%22%20viewBox=%220%200%20%20%22%3E%3C/svg%3E"
    assert URLUtils.clean_url(bad_url) is None


def test_is_onion_url_detects_hidden_services():
    assert URLUtils.is_onion_url("http://exampleexampleexample.onion/") is True
    assert URLUtils.is_onion_url("https://example.com/") is False


def test_clean_url_uses_live_domain_blacklist_reload(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        assert URLUtils.clean_url("https://sub.example.com/path") == "https://sub.example.com/path"

        blacklist_path.write_text("example.com\n", encoding="utf-8")
        assert URLUtils.clean_url("https://sub.example.com/path") is None

        URLUtils.set_blacklist_enabled(False)
        assert URLUtils.clean_url("https://sub.example.com/path") == "https://sub.example.com/path"
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_blacklist_is_seeded_with_default_non_target_domains(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        content = blacklist_path.read_text(encoding="utf-8")
        assert "wikipedia.org" in content
        assert "imdb.com" in content
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_irrelevant_domains_are_auto_persisted_to_blacklist(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        assert URLUtils.is_blacklisted("https://www.imdb.com/title/tt33379543/") is True
        assert "imdb.com" in blacklist_path.read_text(encoding="utf-8")
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_suspicious_cross_domain_ad_redirect_is_detected():
    assert URLUtils.is_suspicious_redirect(
        "https://piracy-site.example/watch/movie",
        "https://doubleclick.net/redirect-ad",
    ) is True


def test_same_site_links_get_higher_priority_than_external_links():
    same_site_priority = URLUtils.get_link_priority(
        "https://piracy-site.example/watch/movie",
        "https://piracy-site.example/download/file",
    )
    external_priority = URLUtils.get_link_priority(
        "https://piracy-site.example/watch/movie",
        "https://random-blog.example/post",
    )

    assert same_site_priority < external_priority


def test_adult_content_domains_are_auto_filtered_and_blacklisted(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        adult_url = "https://bestpornportal.com/sex/videos"

        assert URLUtils.clean_url(adult_url) is None
        assert URLUtils.is_blacklisted(adult_url) is True
        assert "bestpornportal.com" in blacklist_path.read_text(encoding="utf-8")
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_should_queue_link_rejects_adult_cross_domain_targets():
    assert URLUtils.should_queue_link(
        "https://piracy-site.example/watch/movie",
        "https://adult-xxx-videos.com/sex/clip",
    ) is False