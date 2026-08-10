"""Non-blocking execution boundary between asyncio crawler code and the
synchronous `MediaEvidenceStore` protocol (storage/media_evidence_store.py).

Mirrors `core/frontier_executor.py`'s `AsyncFrontier`, which does the same
job for the `Frontier` protocol -- see that module's docstring for the full
rationale. This module exists because
docs/architecture/fetch-extractor-audit.md §8/§14 found the same class of
defect the frontier fix (docs/architecture/frontier-step4.md) already
closed, but for Media Evidence: every crawler engine's `worker()` (and, for
`AsyncCrawler`/`HTTPCrawler`/`TorCrawler`, their `fetch()`'s direct-response
media path) called `record_media_link`/`record_manifest_variants` directly,
with no `await`, no `asyncio.to_thread` -- both `RedisMediaEvidenceStore`
(synchronous `redis-py` network I/O) and `SQLiteMediaEvidenceStore`
(synchronous `sqlite3` disk I/O) block the single OS thread the asyncio
event loop runs on for the duration of that call, freezing every other
concurrently-scheduled coroutine in the process.

Unlike `AsyncFrontier`, which skips the thread hop entirely for the local,
pure in-memory `URLFrontier` backend, `AsyncMediaEvidence` always offloads:
Media Evidence has no backend that is guaranteed non-blocking (SQLite mode
is development-only, not a fleet-wide mirror -- see
docs/architecture/media-evidence-redis-design.md, "Architecture
Boundaries" -- but it still does real synchronous disk I/O). `asyncio.
to_thread` reuses the event loop's shared default `ThreadPoolExecutor`
(bounded, not one thread spawned per call), so this does not grow
unbounded with worker concurrency.

Scope is deliberately narrow: only the two operations actually called from
the crawl-time hot path (`record_media_link`, `record_manifest_variants`)
are wrapped. Every other `MediaEvidenceStore` method (fingerprint job
claim/renew/complete/fail, listing, `clear`/`close`) is a startup/shutdown/
CLI/fingerprinter-worker operation, not part of the per-page crawl hot
path, and stays on the raw synchronous store -- `core/crawler_manager.py`
still calls `self.media_database.clear()`/`.close()` directly, and any
future fingerprinter worker can do the same.

This module does not change the `MediaEvidenceStore` protocol -- it stays
fully synchronous. `AsyncMediaEvidence` is a separate, optional adapter
that crawler code opts into at construction time, exactly like
`AsyncFrontier`.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from storage.media_evidence_store import MediaEvidenceStore


class AsyncMediaEvidence:
    """Async-safe adapter around a synchronous `MediaEvidenceStore` backend.

    Only `record_media_link`/`record_manifest_variants` have async
    counterparts here -- see this module's docstring for why the rest of
    the protocol is intentionally left uncovered.
    """

    def __init__(self, store: MediaEvidenceStore | "AsyncMediaEvidence"):
        if isinstance(store, AsyncMediaEvidence):
            # Idempotent: wrapping an already-wrapped store reuses the same
            # underlying object instead of nesting adapters (which would
            # hand a coroutine function to asyncio.to_thread and silently
            # do the wrong thing) -- mirrors AsyncFrontier's same guard.
            self._store: MediaEvidenceStore = store._store
            return
        self._store = store

    @property
    def raw(self) -> MediaEvidenceStore:
        """Escape hatch to the wrapped synchronous store (e.g. for
        attribute access that isn't a hot-path operation)."""
        return self._store

    async def record_media_link(
        self,
        *,
        url: str,
        source_page: Optional[str] = None,
        referrer_url: Optional[str] = None,
        discovered_by: str = "unknown",
        discovery_method: str = "parser",
        media_type: Optional[str] = None,
        mime_type: Optional[str] = None,
        content_length: Optional[int] = None,
        priority: int = 10,
    ) -> str:
        return await asyncio.to_thread(
            self._store.record_media_link,
            url=url,
            source_page=source_page,
            referrer_url=referrer_url,
            discovered_by=discovered_by,
            discovery_method=discovery_method,
            media_type=media_type,
            mime_type=mime_type,
            content_length=content_length,
            priority=priority,
        )

    async def record_manifest_variants(self, asset_id: str, variants: list[dict]) -> None:
        await asyncio.to_thread(self._store.record_manifest_variants, asset_id, variants)
