"""Search-engine powered URL discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from loguru import logger

from search_engines.ahmia_search import AhmiaSearch
from search_engines.base import BaseSearchEngine, SearchEngineBlockedError, SearchEngineError
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

SURFACE_WEB_ENGINES: tuple[str, ...] = (
    "duckduckgo",
    "bing",
    "brave",
    "yandex",
)

DARK_WEB_ENGINES: tuple[str, ...] = (
    "ahmia",
    "torch",
)

DEFAULT_ENGINE_PRIORITIES: dict[str, int] = {
    "torch": 0,
    "ahmia": 2,
    "brave": 4,
    "bing": 5,
    "duckduckgo": 6,
    "yandex": 7,
}


def get_engine_names_for_scope(scope: str | None, enabled_engines: Sequence[str]) -> list[str]:
    """Return enabled engine names filtered by discovery scope."""

    enabled = list(enabled_engines)
    if scope is None:
        return enabled

    if scope == "surface-web":
        allowed = set(SURFACE_WEB_ENGINES)
    elif scope == "dark-web":
        allowed = set(DARK_WEB_ENGINES)
    else:
        raise ValueError(f"Unknown discovery scope: {scope}")

    return [engine_name for engine_name in enabled if engine_name in allowed]


@dataclass(slots=True)
class DiscoveryURL:
    url: str
    priority: int
    engine: str
    rank: int
    is_onion: bool = False


@dataclass(slots=True)
class QueryDiscoveryReport:
    query: str
    urls: list[str] = field(default_factory=list)
    discovered_items: list[DiscoveryURL] = field(default_factory=list)
    engine_results: dict[str, int] = field(default_factory=dict)
    engine_errors: dict[str, str] = field(default_factory=dict)
    skipped_engines: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryBatchReport:
    urls: list[str] = field(default_factory=list)
    discovered_items: list[DiscoveryURL] = field(default_factory=list)
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


def score_discovered_url(
    url: str,
    *,
    engine_name: str,
    rank: int,
    engine_priorities: dict[str, int] | None = None,
    onion_priority_boost: int = 2,
) -> int:
    """Return a smaller-is-better crawl priority for a discovered URL."""

    priorities = engine_priorities or DEFAULT_ENGINE_PRIORITIES
    priority = priorities.get(engine_name, 10) + min(rank, 10)

    if URLUtils.is_onion_url(url):
        priority = max(0, priority - onion_priority_boost)

    return priority


def discover_urls_from_query_with_report(
    query: str,
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
    engine_priorities: dict[str, int] | None = None,
    onion_priority_boost: int = 2,
    blocked_engines: dict[str, int] | None = None,
) -> QueryDiscoveryReport:
    """Use configured search engines to discover URLs for a single query."""

    seen: set[str] = set()
    report = QueryDiscoveryReport(query=query)

    blocked_engines = blocked_engines or {}

    for engine in build_search_engines(engine_names, timeout=timeout, user_agent=user_agent):
        cooldown_remaining = blocked_engines.get(engine.name, 0)
        if cooldown_remaining > 0:
            report.skipped_engines[engine.name] = f"cooldown active for {cooldown_remaining} more quer{'y' if cooldown_remaining == 1 else 'ies'}"
            continue

        try:
            engine_urls = engine.search(query, max_results=max_results)
            report.engine_results[engine.name] = len(engine_urls)

            for rank, url in enumerate(engine_urls):
                cleaned = URLUtils.clean_url(url)
                if not cleaned or cleaned in seen:
                    continue

                seen.add(cleaned)
                report.urls.append(cleaned)
                report.discovered_items.append(
                    DiscoveryURL(
                        url=cleaned,
                        priority=score_discovered_url(
                            cleaned,
                            engine_name=engine.name,
                            rank=rank,
                            engine_priorities=engine_priorities,
                            onion_priority_boost=onion_priority_boost,
                        ),
                        engine=engine.name,
                        rank=rank,
                        is_onion=URLUtils.is_onion_url(cleaned),
                    )
                )

        except SearchEngineBlockedError as exc:
            report.engine_errors[engine.name] = str(exc)
            logger.warning(f"Search engine {engine.name} failed for {query!r}: {exc}")
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
    engine_priorities: dict[str, int] | None = None,
    onion_priority_boost: int = 2,
) -> List[str]:
    """Return discovered URLs for a single query."""

    return discover_urls_from_query_with_report(
        query,
        max_results=max_results,
        engine_names=engine_names,
        timeout=timeout,
        user_agent=user_agent,
        engine_priorities=engine_priorities,
        onion_priority_boost=onion_priority_boost,
    ).urls


def discover_urls_from_queries_with_report(
    queries: Iterable[str],
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
    engine_priorities: dict[str, int] | None = None,
    onion_priority_boost: int = 2,
    blocked_engine_cooldown_queries: int = 999,
) -> DiscoveryBatchReport:
    """Search multiple queries and return a deduplicated URL report."""

    seen: set[str] = set()
    batch_report = DiscoveryBatchReport()
    blocked_engines: dict[str, int] = {}

    for query in queries:
        blocked_engines = {
            name: remaining - 1
            for name, remaining in blocked_engines.items()
            if remaining > 0
        }

        query_report = discover_urls_from_query_with_report(
            query,
            max_results=max_results,
            engine_names=engine_names,
            timeout=timeout,
            user_agent=user_agent,
            engine_priorities=engine_priorities,
            onion_priority_boost=onion_priority_boost,
            blocked_engines=blocked_engines,
        )
        batch_report.query_reports.append(query_report)

        for engine_name, error in query_report.engine_errors.items():
            if "captcha" in error.lower() or "blocked" in error.lower() or engine_name == "yandex":
                blocked_engines[engine_name] = max(blocked_engine_cooldown_queries, 1)

        for item in sorted(query_report.discovered_items, key=lambda entry: (entry.priority, entry.rank, entry.url)):
            if item.url in seen:
                continue

            seen.add(item.url)
            batch_report.urls.append(item.url)
            batch_report.discovered_items.append(item)

    return batch_report


def discover_urls_from_queries(
    queries: Iterable[str],
    max_results: int = 20,
    engine_names: Sequence[str] | None = None,
    timeout: int = 15,
    user_agent: str | None = None,
    engine_priorities: dict[str, int] | None = None,
    onion_priority_boost: int = 2,
    blocked_engine_cooldown_queries: int = 999,
) -> List[str]:
    """Search multiple queries and return a deduplicated list of URLs."""

    return discover_urls_from_queries_with_report(
        queries,
        max_results=max_results,
        engine_names=engine_names,
        timeout=timeout,
        user_agent=user_agent,
        engine_priorities=engine_priorities,
        onion_priority_boost=onion_priority_boost,
        blocked_engine_cooldown_queries=blocked_engine_cooldown_queries,
    ).urls
