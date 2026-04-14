"""Simple URL persistence layer using SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


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
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO urls (url, first_seen, last_seen, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    status = CASE
                        WHEN urls.status = 'visited' THEN urls.status
                        ELSE excluded.status
                    END""",
            (url, now, now, status),
        )
        self._conn.commit()

    def update_status(self, url: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE urls SET status = ?, last_seen = ? WHERE url = ?""",
            (status, now, url),
        )
        self._conn.commit()

    def update_many_status(self, urls: Sequence[str], status: str) -> None:
        if not urls:
            return

        now = datetime.now(timezone.utc).isoformat()
        self._conn.executemany(
            """UPDATE urls SET status = ?, last_seen = ? WHERE url = ?""",
            [(status, now, url) for url in urls],
        )
        self._conn.commit()

    def is_visited(self, url: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM urls WHERE url = ? AND status = 'visited'", (url,))
        return bool(cur.fetchone())

    def get_all_urls(self) -> Iterable[str]:
        cur = self._conn.execute("SELECT url FROM urls")
        return [row[0] for row in cur.fetchall()]

    def get_urls_by_status(self, statuses: Sequence[str]) -> list[str]:
        if not statuses:
            return []

        placeholders = ", ".join("?" for _ in statuses)
        cur = self._conn.execute(
            f"SELECT url FROM urls WHERE status IN ({placeholders}) ORDER BY last_seen ASC",
            tuple(statuses),
        )
        return [row[0] for row in cur.fetchall()]

    def get_status_counts(self) -> dict[str, int]:
        cur = self._conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")
        return {status: count for status, count in cur.fetchall()}

    def close(self) -> None:
        self._conn.close()
