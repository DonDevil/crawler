"""Seed URL loading utilities."""

from __future__ import annotations

import os
from typing import Iterable


def load_seeds(path: str) -> Iterable[str]:
    """Yield seed URLs from a file.

    Lines starting with `#` or empty lines are ignored.
    """

    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            yield line
