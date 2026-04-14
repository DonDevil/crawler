"""Yandex search engine scraping."""

from __future__ import annotations

from search_engines.base import BaseSearchEngine, SearchEngineBlockedError


class YandexSearch(BaseSearchEngine):
    """Scrape Yandex search result pages when they are accessible."""

    name = "yandex"
    BASE_URL = "https://yandex.com/search/"

    def search(self, query: str, max_results: int = 20) -> list[str]:
        soup, final_url = self._make_soup(self.BASE_URL, params={"text": query})

        if "showcaptcha" in final_url.lower() or soup.title and "verification" in soup.title.get_text(strip=True).lower():
            raise SearchEngineBlockedError(
                "Yandex requires captcha verification for this IP/session; HTML scraping is blocked upstream"
            )

        return self._collect_urls(
            soup,
            selectors=(
                "li.serp-item a[href]",
                "a.OrganicTitle-Link[href]",
                "a.Link[href]",
            ),
            max_results=max_results,
        )
