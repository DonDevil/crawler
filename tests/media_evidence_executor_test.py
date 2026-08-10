"""Tests for core/media_evidence_executor.py's AsyncMediaEvidence adapter.

Demonstrates the same requirement `tests/frontier_executor_test.py` proves
for the frontier: Media Evidence operations invoked from async crawler code
must not execute on the event-loop thread. See
docs/architecture/fetch-extractor-audit.md §8/§14.

Unlike AsyncFrontier, AsyncMediaEvidence always offloads -- there is no
"local, pure in-memory" Media Evidence backend the way URLFrontier is for
the frontier. Both SQLiteMediaEvidenceStore (sqlite3 disk I/O) and
RedisMediaEvidenceStore (redis-py network I/O) do blocking I/O, so both are
covered here.

The Redis-backed tests need a live Redis on localhost:6379 and are skipped
otherwise, consistent with tests/redis_media_evidence_store_test.py.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
import redis

from core.media_evidence_executor import AsyncMediaEvidence
from storage.media_evidence_store import InvalidMediaURLError, MediaEvidenceUnavailable
from storage.redis_media_evidence_store import RedisMediaEvidenceStore
from storage.sqlite_media_evidence_store import SQLiteMediaEvidenceStore


def _current_thread_id() -> int:
    return threading.get_ident()


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteMediaEvidenceStore(path=str(tmp_path / "media_evidence.db"))
    yield store
    store.close()


@pytest.fixture
def redis_store() -> RedisMediaEvidenceStore:
    try:
        store = RedisMediaEvidenceStore(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_evidence_executor",
        )
        store.clear()
        yield store
        store.clear()
        store.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


class TestRecordMediaLinkThroughAdapter:
    """B: record_media_link() works through the adapter, for both backends."""

    @pytest.mark.asyncio
    async def test_sqlite_backend(self, sqlite_store: SQLiteMediaEvidenceStore):
        adapter = AsyncMediaEvidence(sqlite_store)

        asset_id = await adapter.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/1",
            media_type="video",
            priority=6,
        )
        assert asset_id

        # Duplicate discovery returns the same asset id -- confirms the
        # adapter round-trips the real backend, not a stub.
        duplicate_id = await adapter.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/1",
            media_type="video",
            priority=6,
        )
        assert duplicate_id == asset_id
        assert len(sqlite_store.list_media_assets()) == 1

    @pytest.mark.asyncio
    async def test_redis_backend(self, redis_store: RedisMediaEvidenceStore):
        adapter = AsyncMediaEvidence(redis_store)

        asset_id = await adapter.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/1",
            media_type="video",
            priority=6,
        )
        assert asset_id
        assert len(redis_store.list_media_assets()) == 1


class TestRecordManifestVariantsThroughAdapter:
    """C: record_manifest_variants() works through the adapter."""

    @pytest.mark.asyncio
    async def test_sqlite_backend(self, sqlite_store: SQLiteMediaEvidenceStore):
        adapter = AsyncMediaEvidence(sqlite_store)
        asset_id = await adapter.record_media_link(
            url="https://cdn.example/master.m3u8", media_type="stream-manifest", priority=4
        )

        await adapter.record_manifest_variants(
            asset_id,
            [{"url": "https://cdn.example/480p.m3u8", "bandwidth": 800000, "resolution": "854x480"}],
        )

        variants = sqlite_store.list_manifest_variants(asset_id)
        assert len(variants) == 1
        assert variants[0]["variant_url"] == "https://cdn.example/480p.m3u8"

    @pytest.mark.asyncio
    async def test_redis_backend(self, redis_store: RedisMediaEvidenceStore):
        adapter = AsyncMediaEvidence(redis_store)
        asset_id = await adapter.record_media_link(
            url="https://cdn.example/master.m3u8", media_type="stream-manifest", priority=4
        )

        await adapter.record_manifest_variants(
            asset_id,
            [{"url": "https://cdn.example/480p.m3u8", "bandwidth": 800000, "resolution": "854x480"}],
        )

        variants = redis_store.list_manifest_variants(asset_id)
        assert len(variants) == 1
        assert variants[0]["variant_url"] == "https://cdn.example/480p.m3u8"


class TestExceptionsPropagateForNonFatalHandling:
    """D: exceptions retain the existing non-fatal behavior expected by the
    crawler -- every call site wraps the adapter call in its own
    `try/except Exception as exc: logger.debug(...)` (or, for the
    direct-response path in async/http/tor_crawler.py's `fetch()`, relies
    on the surrounding fetch-retry `except Exception`). `asyncio.to_thread`
    must propagate the underlying exception unchanged (type and message),
    not wrap/swallow it, so those call sites keep working exactly as
    before.
    """

    @pytest.mark.asyncio
    async def test_invalid_url_error_propagates_through_the_adapter(self, sqlite_store: SQLiteMediaEvidenceStore):
        adapter = AsyncMediaEvidence(sqlite_store)

        with pytest.raises(InvalidMediaURLError):
            await adapter.record_media_link(url="   ", media_type="video")

    @pytest.mark.asyncio
    async def test_a_raised_exception_does_not_kill_the_event_loop_or_other_tasks(
        self, sqlite_store: SQLiteMediaEvidenceStore
    ):
        """Mirrors the crawler's own call-site pattern: catch the adapter's
        exception locally, non-fatally, exactly like every worker()'s
        `except Exception as exc: logger.debug(...)` block."""
        adapter = AsyncMediaEvidence(sqlite_store)

        caught = None
        try:
            await adapter.record_media_link(url="", media_type="video")
        except Exception as exc:  # noqa: BLE001 -- mirrors the crawler's own catch-all
            caught = exc

        assert isinstance(caught, InvalidMediaURLError)

        # The event loop and adapter are still usable afterward.
        asset_id = await adapter.record_media_link(url="https://cdn.example/still-works.mp4", media_type="video")
        assert asset_id

    @pytest.mark.asyncio
    async def test_media_evidence_unavailable_propagates_through_the_adapter(self):
        """A backend-down failure (MediaEvidenceUnavailable, the Media
        Evidence equivalent of FrontierUnavailable) must reach the caller
        unchanged, not be silently absorbed by the offload boundary."""

        class _BrokenStore:
            def record_media_link(self, **kwargs):
                raise MediaEvidenceUnavailable("redis connection refused")

            def record_manifest_variants(self, asset_id, variants):
                raise MediaEvidenceUnavailable("redis connection refused")

        adapter = AsyncMediaEvidence(_BrokenStore())

        with pytest.raises(MediaEvidenceUnavailable):
            await adapter.record_media_link(url="https://cdn.example/x.mp4")

        with pytest.raises(MediaEvidenceUnavailable):
            await adapter.record_manifest_variants("aid", [])


class TestSqliteLocalModeIsNotBroken:
    """E: SQL/local mode continues working through the adapter -- the same
    backend used by --sql / frontier.type: sqlite development runs."""

    @pytest.mark.asyncio
    async def test_full_record_and_list_round_trip(self, sqlite_store: SQLiteMediaEvidenceStore):
        adapter = AsyncMediaEvidence(sqlite_store)

        for i in range(5):
            await adapter.record_media_link(
                url=f"https://cdn.example/clip-{i}.mp4",
                source_page="https://piracy.example/watch/local",
                media_type="video",
                priority=5,
            )

        assets = sqlite_store.list_media_assets()
        assert len(assets) == 5


class TestOffloadProof:
    """A: calls made through the adapter are executed off the event-loop
    thread, for both backends -- neither has a "guaranteed non-blocking"
    exemption the way the local frontier does."""

    @pytest.mark.asyncio
    async def test_sqlite_record_media_link_runs_off_the_event_loop_thread(
        self, sqlite_store: SQLiteMediaEvidenceStore
    ):
        adapter = AsyncMediaEvidence(sqlite_store)
        loop_thread = _current_thread_id()
        recorded: dict[str, int] = {}

        original = sqlite_store.record_media_link

        def spy(*args, **kwargs):
            recorded["record_media_link"] = _current_thread_id()
            return original(*args, **kwargs)

        sqlite_store.record_media_link = spy

        await adapter.record_media_link(url="https://cdn.example/offload.mp4", media_type="video")

        assert "record_media_link" in recorded
        assert recorded["record_media_link"] != loop_thread

    @pytest.mark.asyncio
    async def test_sqlite_record_manifest_variants_runs_off_the_event_loop_thread(
        self, sqlite_store: SQLiteMediaEvidenceStore
    ):
        adapter = AsyncMediaEvidence(sqlite_store)
        loop_thread = _current_thread_id()
        asset_id = await adapter.record_media_link(
            url="https://cdn.example/offload.m3u8", media_type="stream-manifest"
        )
        recorded: dict[str, int] = {}

        original = sqlite_store.record_manifest_variants

        def spy(*args, **kwargs):
            recorded["record_manifest_variants"] = _current_thread_id()
            return original(*args, **kwargs)

        sqlite_store.record_manifest_variants = spy

        await adapter.record_manifest_variants(
            asset_id, [{"url": "https://cdn.example/offload-480p.m3u8", "bandwidth": 1}]
        )

        assert "record_manifest_variants" in recorded
        assert recorded["record_manifest_variants"] != loop_thread

    @pytest.mark.asyncio
    async def test_redis_record_media_link_runs_off_the_event_loop_thread(
        self, redis_store: RedisMediaEvidenceStore
    ):
        adapter = AsyncMediaEvidence(redis_store)
        loop_thread = _current_thread_id()
        recorded: dict[str, int] = {}

        original_execute_command = redis_store.redis_conn.execute_command

        def spying_execute_command(*args, **kwargs):
            recorded["thread"] = _current_thread_id()
            return original_execute_command(*args, **kwargs)

        redis_store.redis_conn.execute_command = spying_execute_command

        await adapter.record_media_link(url="https://cdn.example/offload.mp4", media_type="video")

        assert "thread" in recorded
        assert recorded["thread"] != loop_thread

    @pytest.mark.asyncio
    async def test_a_slow_media_write_does_not_block_a_concurrent_task(self, sqlite_store: SQLiteMediaEvidenceStore):
        """The whole point of the offload: while one record_media_link call
        is in flight (artificially slowed down), another concurrently-
        scheduled coroutine must still make progress on the event loop."""
        adapter = AsyncMediaEvidence(sqlite_store)

        import time as time_module

        original = sqlite_store.record_media_link

        def slow_record_media_link(*args, **kwargs):
            time_module.sleep(0.3)
            return original(*args, **kwargs)

        sqlite_store.record_media_link = slow_record_media_link

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks += 1

        write_task = asyncio.create_task(
            adapter.record_media_link(url="https://cdn.example/slow.mp4", media_type="video")
        )
        ticker_task = asyncio.create_task(ticker())

        await write_task
        await ticker_task

        # If record_media_link ran inline on the event loop, the 0.3s sleep
        # would have starved the ticker of its ~0.2s of 20ms ticks.
        assert ticks == 10

    @pytest.mark.asyncio
    async def test_concurrent_calls_use_a_bounded_shared_thread_pool(self, sqlite_store: SQLiteMediaEvidenceStore):
        adapter = AsyncMediaEvidence(sqlite_store)
        n_calls = 50
        seen_threads: set[int] = set()

        original = sqlite_store.record_media_link

        def spy(*args, **kwargs):
            seen_threads.add(_current_thread_id())
            return original(*args, **kwargs)

        sqlite_store.record_media_link = spy

        await asyncio.gather(
            *(
                adapter.record_media_link(url=f"https://cdn.example/bulk-{i}.mp4", media_type="video")
                for i in range(n_calls)
            )
        )

        assert 0 < len(seen_threads) <= 32, (
            f"expected a small bounded set of worker threads, saw {len(seen_threads)}"
        )


class TestAsyncMediaEvidenceIdempotency:
    def test_wrapping_an_already_wrapped_store_reuses_the_underlying_object(self, sqlite_store):
        once = AsyncMediaEvidence(sqlite_store)
        twice = AsyncMediaEvidence(once)

        assert twice._store is sqlite_store
        assert twice.raw is sqlite_store

    def test_none_stays_falsy_for_disabled_media_evidence(self):
        # Mirrors how every crawler engine constructs its `media_database`
        # attribute: `AsyncMediaEvidence(x) if x is not None else None`.
        media_database = None
        wrapped = AsyncMediaEvidence(media_database) if media_database is not None else None
        assert wrapped is None
