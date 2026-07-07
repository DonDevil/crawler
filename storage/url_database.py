"""Simple URL persistence layer using SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from storage.async_database_writer import BatchedDatabaseWriter


class URLDatabase:
    """A small SQLite-backed URL store with batched writes."""

    def __init__(self, path: str = "storage/crawl_state.db", batch_size: int = 50):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA cache_size=10000")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._writer = BatchedDatabaseWriter(self._conn, batch_size=batch_size)
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
        self._writer.execute(
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

    def update_status(self, url: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._writer.execute(
            """UPDATE urls SET status = ?, last_seen = ? WHERE url = ?""",
            (status, now, url),
        )

    def update_many_status(self, urls: Sequence[str], status: str) -> None:
        if not urls:
            return

        now = datetime.now(timezone.utc).isoformat()
        for url in urls:
            self._writer.execute(
                """UPDATE urls SET status = ?, last_seen = ? WHERE url = ?""",
                (status, now, url),
            )

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

    def get_urls_and_statuses(self, statuses: Sequence[str]) -> list[tuple[str, str]]:
        if not statuses:
            return []

        placeholders = ", ".join("?" for _ in statuses)
        cur = self._conn.execute(
            f"SELECT url, status FROM urls WHERE status IN ({placeholders}) ORDER BY last_seen ASC",
            tuple(statuses),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]

    def get_status_counts(self) -> dict[str, int]:
        cur = self._conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")
        return {status: count for status, count in cur.fetchall()}

    def clear(self) -> None:
        """Remove all stored URL crawl state from the database."""

        self._writer.execute("DELETE FROM urls")
        self._writer.flush()

    def close(self) -> None:
        self._writer.flush()
        self._conn.close()
