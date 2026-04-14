"""Torch search engine scraping over Tor."""

from __future__ import annotations

from search_engines.base import BaseSearchEngine
from tor.proxy_config import get_default_tor_proxy


class TorchSearch(BaseSearchEngine):
    """Scrape Torch onion search results through a Tor proxy."""

    name = "torch"
    BASE_URL = "http://torch.onion/search"

    def __init__(self, timeout: int = 15, user_agent: str | None = None, proxy: str | None = None):
        super().__init__(timeout=timeout, user_agent=user_agent)
        self.proxy = proxy or get_default_tor_proxy()

    def search(self, query: str, max_results: int = 20) -> list[str]:
        soup, _ = self._make_soup(
            self.BASE_URL,
            params={"query": query},
            proxy=self.proxy,
        )
        return self._collect_urls(
            soup,
            selectors=("a[href*='.onion']", "a[href^='http']"),
            max_results=max_results,
        )
