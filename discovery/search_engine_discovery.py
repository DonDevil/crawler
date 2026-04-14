"""Search-engine powered URL discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from loguru import logger

from search_engines.ahmia_search import AhmiaSearch
from search_engines.base import BaseSearchEngine, SearchEngineError
from search_engines.bing_search import BingSearch
from search_engines.brave_search import BraveSearch
from search_engines.duckduckgo_search import DuckDuckGoSearch
from search_engines.torch_search import TorchSearch
from search_engines.yandex_search import YandexSearch
from utils.url_utils import URLUtils


ENGINE_REGISTRY: dict[str, type[BaseSearchEngine]] = {
    "duckduckgo": DuckDuckGoSearch,
    "bing": BingSearch,
    "brave": BraveSearch,
    "yandex": YandexSearch,
    "ahmia": AhmiaSearch,
    "torch": TorchSearch,
}


@dataclass(slots=True)
class QueryDiscoveryReport:
    query: str
    urls: list[str] = field(default_factory=list)
    engine_results: dict[str, int] = field(default_factory=dict)
    engine_errors: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryBatchReport:
    urls: list[str] = field(default_factory=list)
    query_reports: list[QueryDiscoveryReport] = field(default_factory=list)


def build_search_engines(
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
) -> list[BaseSearchEngine]:
    """Build configured search engine clients."""

    names = list(engine_names or ENGINE_REGISTRY.keys())
    engines: list[BaseSearchEngine] = []

    for name in names:
        engine_class = ENGINE_REGISTRY.get(name)
        if engine_class is None:
            logger.warning(f"Ignoring unknown search engine: {name}")
            continue
        engines.append(engine_class(timeout=timeout, user_agent=user_agent))

    return engines


def discover_urls_from_query_with_report(
    query: str,
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
) -> QueryDiscoveryReport:
    """Use configured search engines to discover URLs for a single query."""

    seen: set[str] = set()
    report = QueryDiscoveryReport(query=query)

    for engine in build_search_engines(engine_names, timeout=timeout, user_agent=user_agent):
        try:
            engine_urls = engine.search(query, max_results=max_results)
            report.engine_results[engine.name] = len(engine_urls)

            for url in engine_urls:
                cleaned = URLUtils.clean_url(url)
                if not cleaned or cleaned in seen:
                    continue

                seen.add(cleaned)
                report.urls.append(cleaned)

        except SearchEngineError as exc:
            report.engine_errors[engine.name] = str(exc)
            logger.warning(f"Search engine {engine.name} failed for {query!r}: {exc}")
        except Exception as exc:  # pragma: no cover - defensive logging path
            report.engine_errors[engine.name] = f"unexpected error: {exc}"
            logger.exception(f"Unexpected search discovery error for {engine.name}: {exc}")

    return report


def discover_urls_from_query(
    query: str,
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
) -> List[str]:
    """Return discovered URLs for a single query."""

    return discover_urls_from_query_with_report(
        query,
        max_results=max_results,
        engine_names=engine_names,
        timeout=timeout,
        user_agent=user_agent,
    ).urls


def discover_urls_from_queries_with_report(
    queries: Iterable[str],
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
) -> DiscoveryBatchReport:
    """Search multiple queries and return a deduplicated URL report."""

    seen: set[str] = set()
    batch_report = DiscoveryBatchReport()

    for query in queries:
        query_report = discover_urls_from_query_with_report(
            query,
            max_results=max_results,
            engine_names=engine_names,
            timeout=timeout,
            user_agent=user_agent,
        )
        batch_report.query_reports.append(query_report)

        for url in query_report.urls:
            if url in seen:
                continue

            seen.add(url)
            batch_report.urls.append(url)

    return batch_report


def discover_urls_from_queries(
    queries: Iterable[str],
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
) -> List[str]:
    """Search multiple queries and return a deduplicated list of URLs."""

    return discover_urls_from_queries_with_report(
        queries,
        max_results=max_results,
        engine_names=engine_names,
        timeout=timeout,
        user_agent=user_agent,
    ).urls
