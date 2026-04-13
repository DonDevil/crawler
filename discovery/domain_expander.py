"""Utilities for expanding and normalizing domains."""

from __future__ import annotations

from typing import Iterable

import tldextract


def extract_root_domain(url: str) -> str | None:
    """Extract the root domain (e.g. example.com) from a URL."""

    try:
        extracted = tldextract.extract(url)
        if not extracted.domain or not extracted.suffix:
            return None
        return f"{extracted.domain}.{extracted.suffix}"
    except Exception:
        return None


def expand_domains(urls: Iterable[str]) -> set[str]:
    """Expand a list of URLs into their root domains."""

    domains: set[str] = set()
    for url in urls:
        root = extract_root_domain(url)
        if root:
            domains.add(root)
    return domains
