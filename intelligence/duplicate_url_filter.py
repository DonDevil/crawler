"""Duplicate URL filtering utilities."""

from __future__ import annotations

from typing import Iterable


class DuplicateURLFilter:
    """Simple in-memory duplicate URL tracker."""

    def __init__(self):
        self._seen: set[str] = set()

    def filter(self, urls: Iterable[str]) -> list[str]:
        """Return a list of URLs with duplicates removed, preserving order."""
        out: list[str] = []
        for url in urls:
            if url not in self._seen:
                self._seen.add(url)
                out.append(url)
        return out
