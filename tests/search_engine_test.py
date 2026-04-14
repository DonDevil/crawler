"""Tests for search engine URL cleanup behavior."""

from search_engines.bing_search import BingSearch


def test_bing_search_decodes_redirect_urls():
    engine = BingSearch()
    wrapped = (
        "https://www.bing.com/ck/a?u="
        "a1aHR0cHM6Ly9lbi5tLndpa2lwZWRpYS5vcmcvd2lraS9KYW5hX05heWFnYW4"
    )

    assert engine.clean_result_url(wrapped) == "https://en.m.wikipedia.org/wiki/Jana_Nayagan"