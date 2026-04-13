"""Discovery utilities that use search engines to find new URLs."""

from __future__ import annotations

from typing import Iterable, List

from search_engines.duckduckgo_search import DuckDuckGoSearch


def discover_urls_from_query(query: str, max_results: int = 20) -> List[str]:
    """Use a search engine to discover URLs for a given query."""

    engine = DuckDuckGoSearch()
    return engine.search(query, max_results=max_results)


def discover_urls_from_queries(queries: Iterable[str], max_results: int = 20) -> List[str]:
    """Search multiple queries and return a deduplicated list of URLs."""

    seen: set[str] = set()
    results: List[str] = []

    for query in queries:
        for url in discover_urls_from_query(query, max_results=max_results):
            if url not in seen:
                seen.add(url)
                results.append(url)

    return results
