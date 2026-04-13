"""Torrent site discovery utilities."""

from __future__ import annotations

from typing import Iterable

from discovery.piracy_site_seeds import load_seeds


def load_torrent_sites(path: str) -> Iterable[str]:
    """Load torrent site seeds from a file."""

    for url in load_seeds(path):
        yield url
