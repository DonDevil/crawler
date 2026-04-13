"""Crawl state persistence.

This module provides a small key/value store for keeping crawler run state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional


class CrawlStateDB:
    """Store and retrieve small state values related to crawler runs."""

    def __init__(self, path: str = "storage/crawl_state.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        self._conn.commit()

    def set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)"
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def close(self) -> None:
        self._conn.close()
