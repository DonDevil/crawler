"""Dark web discovery utilities."""

from __future__ import annotations

from typing import Iterable

from discovery.piracy_site_seeds import load_seeds


def load_onion_seeds(path: str) -> Iterable[str]:
    """Load .onion seed URLs from a seed file."""

    for url in load_seeds(path):
        if url.lower().endswith(".onion") or ".onion/" in url.lower():
            yield url
