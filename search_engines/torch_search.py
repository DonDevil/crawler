"""Torch search engine scraping over Tor."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from search_engines.base import BaseSearchEngine, SearchEngineUnavailableError
from tor.proxy_config import get_default_tor_proxy
from utils.url_utils import URLUtils


class TorchSearch(BaseSearchEngine):
    """Scrape Torch onion search results through a Tor proxy."""

    name = "torch"
    BASE_URLS = (
        "http://torchqfmuhpqteg5nww33wztcfxcly2rl3kwsk6zxja7gi5awgsk7qad.onion/",
        "http://torchs7vpa4w6ddmgj56yseropeo5y47ixktki57a45l7zmwxrffnnqd.onion/",
        "http://torchac4wwv4sd3qt73xjxvz6wxiande4mhsvbhi4icsui7lgwh4kbqd.onion/",
    )

    def __init__(self, timeout: int = 15, user_agent: str | None = None, proxy: str | None = None):
        super().__init__(timeout=timeout, user_agent=user_agent)
        self.proxy = proxy or get_default_tor_proxy()
        self._mirror_hosts = {urlparse(url).netloc for url in self.BASE_URLS}

    def clean_result_url(self, url: str) -> str | None:
        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            return None

        parsed = urlparse(cleaned)
        if parsed.netloc in self._mirror_hosts and parsed.path in {"/", "/index.htm", "/advertise.htm", "/search.htm"}:
            return None

        return cleaned

    def search(self, query: str, max_results: int = 20) -> list[str]:
        errors: list[str] = []

        for base_url in self.BASE_URLS:
            try:
                soup, final_url = self._make_soup(
                    urljoin(base_url, "search.htm"),
                    params={"P": query, "DEFAULTOP": "and"},
                    proxy=self.proxy,
                )
                urls = self._collect_urls(
                    soup,
                    selectors=("a[href]",),
                    max_results=max_results,
                    base_url=final_url,
                )
                if urls:
                    return urls
                errors.append(f"{base_url} returned no usable results")
            except SearchEngineUnavailableError as exc:
                errors.append(f"{base_url}: {exc}")

        raise SearchEngineUnavailableError("; ".join(errors) or "no working Torch mirror found")
