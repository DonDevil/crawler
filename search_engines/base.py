"""Shared search engine client helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from utils.request_headers import get_default_headers
from utils.url_utils import URLUtils


class SearchEngineError(RuntimeError):
    """Base error raised when a search engine query fails."""


class SearchEngineUnavailableError(SearchEngineError):
    """Raised when an engine cannot be reached or returns an unusable response."""


class SearchEngineBlockedError(SearchEngineError):
    """Raised when an engine blocks automated access or requires verification."""


class SearchEngineParsingError(SearchEngineError):
    """Raised when a result page structure cannot be parsed."""


class BaseSearchEngine(ABC):
    """Common functionality for scraping HTML search result pages."""

    name = "base"

    def __init__(self, timeout: int = 15, user_agent: str | None = None):
        self.timeout = timeout
        self.user_agent = user_agent

    def _build_headers(self) -> dict[str, str]:
        headers = get_default_headers(self.user_agent)
        headers["Accept-Encoding"] = "identity"
        return headers

    def _fetch_html(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> tuple[str, str]:
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers=self._build_headers(),
                proxy=proxy,
            ) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response.text, str(response.url)
        except httpx.HTTPStatusError as exc:
            raise SearchEngineUnavailableError(
                f"HTTP {exc.response.status_code} from {self.name}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchEngineUnavailableError(str(exc)) from exc

    def _make_soup(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> tuple[BeautifulSoup, str]:
        html, final_url = self._fetch_html(url, params=params, proxy=proxy)
        return BeautifulSoup(html, "lxml"), final_url

    def _collect_urls(
        self,
        soup: BeautifulSoup,
        selectors: tuple[str, ...],
        max_results: int,
        base_url: str | None = None,
    ) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []

        for selector in selectors:
            for anchor in soup.select(selector):
                href = anchor.get("href")
                if not href:
                    continue

                candidate = urljoin(base_url, href) if base_url else href
                cleaned = self.clean_result_url(candidate)
                if not cleaned or cleaned in seen:
                    continue

                seen.add(cleaned)
                urls.append(cleaned)
                if len(urls) >= max_results:
                    return urls

        return urls

    def clean_result_url(self, url: str) -> str | None:
        return URLUtils.clean_url(url)

    @abstractmethod
    def search(self, query: str, max_results: int = 20) -> list[str]:
        """Run a query against this engine and return result URLs."""