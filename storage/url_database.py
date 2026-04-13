"""Simple URL persistence layer using SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


class URLDatabase:
    """A small SQLite-backed URL store."""

    def __init__(self, path: str = "storage/crawl_state.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self):
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS urls (
                url TEXT PRIMARY KEY,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                status TEXT
            )"""
        )
        self._conn.commit()

    def add_url(self, url: str, status: str = "pending") -> None:
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            """INSERT OR IGNORE INTO urls (url, first_seen, last_seen, status)
                VALUES (?, ?, ?, ?)""",
            (url, now, now, status),
        )
        self._conn.commit()

    def update_status(self, url: str, status: str) -> None:
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            """UPDATE urls SET status = ?, last_seen = ? WHERE url = ?""",
            (status, now, url),
        )
        self._conn.commit()

    def is_visited(self, url: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM urls WHERE url = ? AND status = 'visited'", (url,))
        return bool(cur.fetchone())

    def get_all_urls(self) -> Iterable[str]:
        cur = self._conn.execute("SELECT url FROM urls")
        return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
