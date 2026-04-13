"""DuckDuckGo search engine scraping.

This module provides a lightweight way to search DuckDuckGo and extract result URLs.
"""

from __future__ import annotations

import re
from typing import List

import httpx


class DuckDuckGoSearch:
    """Simple DuckDuckGo search scraper."""

    BASE_URL = "https://html.duckduckgo.com/html/"

    def search(self, query: str, max_results: int = 20) -> List[str]:
        """Perform a search query and return a list of result URLs."""

        params = {"q": query}
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AntiPiracyBot/1.0; +https://example.com/bot)",
        }

        try:
            resp = httpx.get(self.BASE_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception:
            return []

        # DuckDuckGo HTML search renders results in <a class="result__a" ...>
        # But in the HTML endpoint, links are in <a rel="nofollow" class="result__a" ...>

        urls: List[str] = []
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*class="result__a"', resp.text):
            url = match.group(1)
            if url.startswith("/l/?kh="):
                # DuckDuckGo redirect URLs; try to extract actual URL
                m = re.search(r"uddg=([^&]+)", url)
                if m:
                    from urllib.parse import unquote

                    url = unquote(m.group(1))
            urls.append(url)
            if len(urls) >= max_results:
                break

        return urls
