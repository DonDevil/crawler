"""Brave Search result scraping."""

from __future__ import annotations

from search_engines.base import BaseSearchEngine


class BraveSearch(BaseSearchEngine):
    """Scrape Brave Search result pages."""

    name = "brave"
    BASE_URL = "https://search.brave.com/search"

    def search(self, query: str, max_results: int = 20) -> list[str]:
        soup, _ = self._make_soup(self.BASE_URL, params={"q": query, "source": "web"})
        return self._collect_urls(
            soup,
            selectors=(
                "div.heading a[href]",
                "div.snippet a[href]",
                "h2 a[href]",
            ),
            max_results=max_results,
        )
