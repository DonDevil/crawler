"""Ahmia search engine connector."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from search_engines.base import BaseSearchEngine, SearchEngineParsingError
from utils.url_utils import URLUtils


class AhmiaSearch(BaseSearchEngine):
    """Scrape Ahmia's public search endpoint for onion results."""

    name = "ahmia"
    HOME_URL = "https://ahmia.fi/"
    SEARCH_URL = "https://ahmia.fi/search/"

    def clean_result_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        redirect_url = parse_qs(parsed.query).get("redirect_url", [None])[0]
        if redirect_url:
            return URLUtils.clean_url(redirect_url, apply_blacklist=False)
        return URLUtils.clean_url(url, apply_blacklist=False)

    def search(self, query: str, max_results: int = 20) -> list[str]:
        home_soup, _ = self._make_soup(self.HOME_URL)
        form = home_soup.find("form", action="/search/")
        if form is None:
            raise SearchEngineParsingError("Ahmia search form not found")

        params = {"q": query}
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if not name or input_tag.get("type") != "hidden":
                continue
            params[name] = input_tag.get("value", "")

        soup, final_url = self._make_soup(self.SEARCH_URL, params=params)
        return self._collect_urls(
            soup,
            selectors=("a[href*='redirect_url=']", "a[href*='.onion']"),
            max_results=max_results,
            base_url=final_url,
        )
