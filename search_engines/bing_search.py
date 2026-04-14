"""Bing search engine scraping."""

from __future__ import annotations

from search_engines.base import BaseSearchEngine


class BingSearch(BaseSearchEngine):
    """Scrape public Bing result pages."""

    name = "bing"
    BASE_URL = "https://www.bing.com/search"

    def search(self, query: str, max_results: int = 20) -> list[str]:
        soup, _ = self._make_soup(self.BASE_URL, params={"q": query})
        return self._collect_urls(
            soup,
            selectors=("li.b_algo h2 a[href]", "li.b_algo a[href]"),
            max_results=max_results,
        )
