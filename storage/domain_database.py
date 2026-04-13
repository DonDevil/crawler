"""Simple domain-level database storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional


class DomainDatabase:
    """Store per-domain metadata for analysis."""

    def __init__(self, path: str = "storage/crawl_state.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS domains (
                domain TEXT PRIMARY KEY,
                first_seen TEXT,
                last_seen TEXT,
                score REAL
            )"""
        )
        self._conn.commit()

    def add_or_update(self, domain: str, score: float = 0.0) -> None:
        self._conn.execute(
            """INSERT INTO domains (domain, first_seen, last_seen, score)
                VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(domain) DO UPDATE SET last_seen = CURRENT_TIMESTAMP, score = excluded.score""",
            (domain, score),
        )
        self._conn.commit()

    def get_score(self, domain: str) -> Optional[float]:
        cur = self._conn.execute("SELECT score FROM domains WHERE domain = ?", (domain,))
        row = cur.fetchone()
        return row[0] if row else None

    def list_domains(self) -> Iterable[str]:
        cur = self._conn.execute("SELECT domain FROM domains")
        return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
