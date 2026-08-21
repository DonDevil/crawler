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


def test_missing_blacklist_file_is_created(tmp_path):
    blacklist_path = tmp_path / "nested" / "domain_blacklist.txt"
    assert not blacklist_path.exists()

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        assert blacklist_path.exists()
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_checking_blacklist_does_not_modify_existing_file(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("blocked.example\n", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        # Seed once so any first-touch bookkeeping (default seeding) settles.
        URLUtils.is_blacklisted("https://harmless.example/page")
        mtime_before = blacklist_path.stat().st_mtime_ns
        content_before = blacklist_path.read_text(encoding="utf-8")

        for _ in range(20):
            URLUtils.is_blacklisted("https://harmless.example/page")
            URLUtils.is_blacklisted("https://blocked.example/other")

        assert blacklist_path.stat().st_mtime_ns == mtime_before
        assert blacklist_path.read_text(encoding="utf-8") == content_before
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_repeated_is_blacklisted_calls_do_not_force_file_reload(tmp_path, monkeypatch):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("blocked.example\n", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled
    real_open = open

    read_calls = {"count": 0}

    def counting_open(file, mode="r", *args, **kwargs):
        if str(file) == str(blacklist_path) and "r" in mode and "+" not in mode:
            read_calls["count"] += 1
        return real_open(file, mode, *args, **kwargs)

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        # Let default-domain seeding (and its own reload) settle before counting,
        # so the cache is already warm when we start measuring.
        URLUtils.is_blacklisted("https://harmless.example/page")

        monkeypatch.setattr("utils.url_utils.open", counting_open, raising=False)

        for _ in range(10):
            URLUtils.is_blacklisted("https://harmless.example/page")
            URLUtils.is_blacklisted("https://blocked.example/other")

        assert read_calls["count"] == 0
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_ad_tracking_hostname_pattern_is_still_auto_blacklisted(tmp_path):
    """Requirement 1: legitimate ad/tracking infrastructure must still be
    auto-blacklisted by hostname pattern, not just by the static defaults list."""
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        tracker_url = "https://ad-tracker-network.example/pixel.gif"

        should_blacklist, reason = URLUtils.classify_auto_blacklist(tracker_url)
        assert should_blacklist is True
        assert reason == "ad_infra_hostname_pattern"

        assert URLUtils.is_blacklisted(tracker_url) is True
        assert "ad-tracker-network.example" in blacklist_path.read_text(encoding="utf-8")
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_piracy_target_domain_is_not_auto_blacklisted_for_being_piracy_content(tmp_path):
    """Requirement 2: a domain must not be auto-blacklisted merely because it
    looks like a piracy/media target (movie, watch, download, pirate, ...)."""
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        piracy_url = "https://best-movies-download.example/pirate/watch/full-movie"

        assert URLUtils.should_auto_blacklist(piracy_url) is False
        assert URLUtils.is_blacklisted(piracy_url) is False
        assert "best-movies-download.example" not in blacklist_path.read_text(encoding="utf-8")
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_self_hosted_ad_redirect_subdomain_does_not_blacklist_piracy_root_domain(tmp_path):
    """Requirement 3 / regression test for the moviesdatamil.co incident:
    an ad/redirect subdomain discovered on a piracy site's own domain (e.g.
    "click.<site>" used for outbound ad redirects) must be blacklisted by its
    exact hostname only -- it must never propagate up to the registered
    domain and take the whole (otherwise legitimate) target site down with it."""
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        tracker_url = "https://click.piracy-target.example/out?to=movie"
        content_url = "https://piracy-target.example/movie/full-download"

        assert URLUtils.is_blacklisted(tracker_url) is True
        assert URLUtils.is_blacklisted(content_url) is False

        lines = {line.strip() for line in blacklist_path.read_text(encoding="utf-8").splitlines()}
        assert "click.piracy-target.example" in lines
        assert "piracy-target.example" not in lines
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


def test_actual_blacklist_file_modification_is_detected_and_reloaded(tmp_path):
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))
        URLUtils.set_blacklist_enabled(True)

        assert URLUtils.is_blacklisted("https://sub.newly-blocked.example/page") is False

        blacklist_path.write_text("newly-blocked.example\n", encoding="utf-8")

        assert URLUtils.is_blacklisted("https://sub.newly-blocked.example/page") is True
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)