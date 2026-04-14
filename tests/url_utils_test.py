"""Tests for URL validation and normalization helpers."""

from utils.url_utils import URLUtils


def test_clean_url_rejects_single_label_hosts():
    assert URLUtils.clean_url("http://search/?q=news") is None


def test_clean_url_rejects_markup_artifacts():
    bad_url = "http://www.w3.org/2000/svg%22%20viewBox=%220%200%20%20%22%3E%3C/svg%3E"
    assert URLUtils.clean_url(bad_url) is None


def test_is_onion_url_detects_hidden_services():
    assert URLUtils.is_onion_url("http://exampleexampleexample.onion/") is True
    assert URLUtils.is_onion_url("https://example.com/") is False