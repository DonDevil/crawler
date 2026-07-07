"""Batched database writer that reduces commit frequency for concurrent access."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(slots=True)
class WriteOperation:
    """Represents a database write operation to batch."""

    sql: str
    params: tuple[Any, ...] | None = None


class BatchedDatabaseWriter:
    """Reduces database commit frequency by batching write operations.

    Collects multiple write operations and commits them together to minimize
    disk sync overhead. Thread-safe using a lock.
    """

    def __init__(self, conn: sqlite3.Connection, batch_size: int = 50):
        """Initialize the batched database writer.

        Args:
            conn: SQLite connection to write through
            batch_size: Number of operations before forced commit (default 50)
        """
        self.conn = conn
        self.batch_size = batch_size
        self._batch: list[WriteOperation] = []
        self._lock = threading.Lock()
        self._operation_count = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        """Queue a write operation to be executed in a batch.

        Automatically commits if batch size reached.
        """
        with self._lock:
            self._batch.append(WriteOperation(sql=sql, params=params))
            if len(self._batch) >= self.batch_size:
                self._flush()

    def flush(self) -> None:
        """Explicitly flush all pending operations and commit."""
        with self._lock:
            self._flush()

    def _flush(self) -> None:
        """Execute all operations in batch and commit (must be called with lock held)."""
        if not self._batch:
            return

        try:
            for operation in self._batch:
                if operation.params:
                    self.conn.execute(operation.sql, operation.params)
                else:
                    self.conn.execute(operation.sql)

            self.conn.commit()
            self._operation_count += len(self._batch)
            self._batch.clear()
        except sqlite3.OperationalError:
            self.conn.rollback()
            self._batch.clear()
            raise
