"""DuckDuckGo search engine scraping."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from search_engines.base import BaseSearchEngine
from utils.url_utils import URLUtils


class DuckDuckGoSearch(BaseSearchEngine):
    """DuckDuckGo HTML search scraper."""

    name = "duckduckgo"
    BASE_URL = "https://html.duckduckgo.com/html/"

    def clean_result_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.path.startswith("/l/"):
            redirect_url = parse_qs(parsed.query).get("uddg", [None])[0]
            if redirect_url:
                return URLUtils.clean_url(redirect_url)
        return URLUtils.clean_url(url)

    def search(self, query: str, max_results: int = 20) -> list[str]:
        soup, final_url = self._make_soup(self.BASE_URL, params={"q": query})
        return self._collect_urls(
            soup,
            selectors=("a.result__a[href]",),
            max_results=max_results,
            base_url=final_url,
        )
