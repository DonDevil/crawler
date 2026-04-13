"""Classify domains as likely piracy-related."""

from __future__ import annotations

from pathlib import Path
from typing import Set


class PiracyDomainClassifier:
    """Simple classifier that checks if a domain is in a known blacklist."""

    def __init__(self, blacklist_path: str = "datasets/known_pirate_domains.txt"):
        self.blacklist_path = Path(blacklist_path)
        self._blacklist: Set[str] = set()
        self._load_blacklist()

    def _load_blacklist(self) -> None:
        if not self.blacklist_path.exists():
            return

        with open(self.blacklist_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self._blacklist.add(line.lower())

    def is_piracy_domain(self, domain: str) -> bool:
        return domain.lower() in self._blacklist
