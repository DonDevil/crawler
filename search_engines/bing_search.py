"""Bing search engine scraping."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

from search_engines.base import BaseSearchEngine
from utils.url_utils import URLUtils


class BingSearch(BaseSearchEngine):
    """Scrape public Bing result pages."""

    name = "bing"
    BASE_URL = "https://www.bing.com/search"

    def clean_result_url(self, url: str) -> str | None:
        parsed = urlparse(url)

        if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/a"):
            encoded_target = parse_qs(parsed.query).get("u", [None])[0]
            if encoded_target:
                if encoded_target.startswith("a1"):
                    encoded_target = encoded_target[2:]

                try:
                    padded = encoded_target + "=" * (-len(encoded_target) % 4)
                    decoded_target = base64.urlsafe_b64decode(padded).decode("utf-8")
                    return URLUtils.clean_url(decoded_target)
                except Exception:
                    return None

        return URLUtils.clean_url(url)

    def search(self, query: str, max_results: int = 20) -> list[str]:
        soup, _ = self._make_soup(self.BASE_URL, params={"q": query})
        return self._collect_urls(
            soup,
            selectors=("li.b_algo h2 a[href]", "li.b_algo a[href]"),
            max_results=max_results,
        )
