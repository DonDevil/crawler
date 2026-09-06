"""Proves the crawler side of the Redis namespace ownership boundary
documented in docs/architecture/history/clear-db-ownership-audit.md:

    crawler:*     -- owned by RedisURLFrontier, cleared by RedisURLFrontier.clear()
    evidence:*    -- owned by RedisMediaEvidenceStore, cleared by RedisMediaEvidenceStore.clear()
    fingerprint:* -- owned by the sibling fingerprinter repo, cleared by its
                     own `target.cli clear-db` -- NEVER by anything in this repo.

`CrawlerManager.clear_storage()` (what `python main.py --clear-db` actually
calls) is exactly `RedisURLFrontier.clear()` + `RedisMediaEvidenceStore.clear()`
when Redis-backed (core/crawler_manager.py). This test seeds all three
namespaces side by side in one Redis db (the same physical instance/db the
bridge's own former `--clear-db` used to sweep across, per the removed
bridge/redis_reset.py's docstring) and proves those two clear() calls only
ever remove their own prefix, never `fingerprint:*` -- the exact boundary
whose violation (via the now-removed bridge `--clear-db`) crashed a live
fingerprinter worker.

This repo deliberately never imports the sibling fingerprinter repo (see
bridge/fingerprint_stream_adapter.py's module docstring), so the reverse
direction -- the fingerprinter's own `work_queue/admin.py::clear_all()`
never touching `crawler:*`/`evidence:*` -- belongs in that repo's own test
suite, not here.

Run against a real, isolated local Redis (test db 1, this repo's existing
convention -- see tests/report_test.py/tests/redis_frontier_test.py). Skips
cleanly if Redis isn't reachable. Never touches db 0.
"""

from __future__ import annotations

import uuid

import pytest
import redis

from core.redis_frontier import RedisURLFrontier
from storage.redis_media_evidence_store import RedisMediaEvidenceStore

_TEST_DB = 1


def _unique_namespace(tag: str) -> str:
    return f"test_ownership_{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def redis_conn():
    try:
        conn = redis.Redis(host="localhost", port=6379, db=_TEST_DB, decode_responses=True)
        conn.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")
    yield conn
    conn.close()


def _seed_synthetic_fingerprint_keys(conn: "redis.Redis", n: int = 5) -> list[str]:
    """Synthetic `fingerprint:*` keys standing in for the fingerprinter's
    own schema (jobs/results/retries/matches/targets) -- this repo does not
    construct real fingerprinter objects (no cross-repo import), so plain
    key/value pairs under the real prefix are sufficient to prove the
    crawler's own clear() calls never touch it."""
    keys = [f"fingerprint:job:synthetic-{i}:state" for i in range(n)]
    for key in keys:
        conn.set(key, "1")
    return keys


@pytest.mark.parametrize("include_targets", [False, True])
def test_redis_frontier_clear_never_touches_fingerprint_namespace(redis_conn, include_targets):
    namespace = _unique_namespace("frontier")
    fp_keys = _seed_synthetic_fingerprint_keys(redis_conn)

    frontier = RedisURLFrontier(
        redis_host="localhost", redis_port=6379, redis_db=_TEST_DB, namespace=namespace, rate_limit=0,
    )
    try:
        frontier.add_url("https://example-ownership-test.test/a")
        frontier.add_url("https://example-ownership-test.test/b")
        assert redis_conn.scard(f"{namespace}:urls:known") == 2

        frontier.clear()

        # This namespace's own state is gone...
        assert redis_conn.scard(f"{namespace}:urls:known") == 0
        # ...but the fingerprinter's namespace is completely untouched.
        for key in fp_keys:
            assert redis_conn.exists(key) == 1, f"{key} was deleted by RedisURLFrontier.clear()!"
    finally:
        frontier.close()
        for key in fp_keys:
            redis_conn.delete(key)


def test_redis_media_evidence_store_clear_never_touches_fingerprint_namespace(redis_conn):
    namespace = _unique_namespace("evidence")
    fp_keys = _seed_synthetic_fingerprint_keys(redis_conn)

    store = RedisMediaEvidenceStore(
        redis_host="localhost", redis_port=6379, redis_db=_TEST_DB, namespace=namespace,
    )
    try:
        store.record_media_link(
            url="https://example-ownership-test.test/movie.mp4",
            source_page="https://example-ownership-test.test/",
            referrer_url="https://example-ownership-test.test/",
            discovered_by="test",
            discovery_method="test",
            media_type="video",
            mime_type="video/mp4",
            priority=5,
        )
        assert redis_conn.scan(match=f"{namespace}:*", count=1000)[1], "seed asset was not written"

        store.clear()

        assert redis_conn.scan(match=f"{namespace}:*", count=1000)[1] == []
        for key in fp_keys:
            assert redis_conn.exists(key) == 1, f"{key} was deleted by RedisMediaEvidenceStore.clear()!"
    finally:
        store.close()
        for key in fp_keys:
            redis_conn.delete(key)


def test_crawler_and_evidence_namespaces_are_independently_scoped(redis_conn):
    """A crawler-side clear() must only ever remove its own `{namespace}:*`
    prefix -- clearing the frontier namespace must never remove the
    media-evidence namespace's keys (a distinct crawler-owned schema in its
    own right), and vice versa. Both are crawler-owned, but they are not
    the same namespace and a clear of one is not a clear of the other."""
    frontier_ns = _unique_namespace("frontier2")
    evidence_ns = _unique_namespace("evidence2")

    frontier = RedisURLFrontier(
        redis_host="localhost", redis_port=6379, redis_db=_TEST_DB, namespace=frontier_ns, rate_limit=0,
    )
    store = RedisMediaEvidenceStore(
        redis_host="localhost", redis_port=6379, redis_db=_TEST_DB, namespace=evidence_ns,
    )
    try:
        frontier.add_url("https://example-ownership-test.test/c")
        store.record_media_link(
            url="https://example-ownership-test.test/clip.mp4",
            source_page="https://example-ownership-test.test/",
            referrer_url="https://example-ownership-test.test/",
            discovered_by="test",
            discovery_method="test",
            media_type="video",
            mime_type="video/mp4",
            priority=5,
        )

        frontier.clear()

        assert redis_conn.scard(f"{frontier_ns}:urls:known") == 0
        assert redis_conn.scan(match=f"{evidence_ns}:*", count=1000)[1] != [], (
            "RedisURLFrontier.clear() must never touch the media evidence namespace"
        )
    finally:
        frontier.close()
        store.clear()
        store.close()
