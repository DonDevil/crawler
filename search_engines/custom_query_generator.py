"""Custom query generator for use with search engines."""

from __future__ import annotations

from typing import List


class CustomQueryGenerator:
    """Generate search queries for discovery."""

    def generate(self, base_terms: List[str]) -> List[str]:
        """Return a list of search queries."""
        return base_terms
