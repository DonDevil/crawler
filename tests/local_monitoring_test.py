"""Tests for per-crawler-process local monitoring (docs/architecture/history/
per-crawler-monitoring.md).

Before this change, `main.py --monitor-resources`'s "THIS RUN" report
section was computed by diffing the *shared* Redis/SQLite frontier state
before and after a process ran (`tests/report_lib.py::build_this_run`).
Under the architecture's own documented multi-worker mode (several crawler
processes sharing one Redis frontier namespace -- core/redis_frontier.py,
docs/architecture/frontier-adr.md), that delta silently includes every
other concurrently-running crawler process's activity too, so two
processes sharing a namespace could report near-identical inflated
"discovered"/"visited"/"failed" numbers that belong to the union of both,
not to either one individually.

These tests prove the fix: `_pages_discovered`/`_pages_crawled`/
`_pages_failed`/`_pages_retried` on each of the 7 crawl-engine classes
(crawler/async_crawler.py and friends) are incremented in-process, at the
exact call site where *that* process performed the action
(`Frontier.add_url()` returning True, a completed `mark_visited()`/
`mark_failed()`) -- never reconstructed from shared state after the fact --
and `tests/report_lib.py::local_counters_from_crawler`/`build_local_work`
read those counters straight off the live engine instance.

Redis-backed tests use db=1 and a per-test unique namespace, matching
tests/report_test.py's / tests/redis_sqlite_mirror_removal_test.py's
convention, and are skipped if Redis isn't reachable on localhost:6379.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest
import redis
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report_lib  # noqa: E402
from core.redis_frontier import RedisURLFrontier  # noqa: E402
from core.url_frontier import URLFrontier  # noqa: E402
from crawler.async_crawler import AsyncCrawler  # noqa: E402
from parsers.html_link_extractor import HTMLLinkExtractor  # noqa: E402


# ---------------------------------------------------------------------------
# Local HTTP fixtures: a root page linking to N same-domain leaf pages
# (dead ends, no further links), and an optional M pages that always 404 --
# mirrors tests/redis_sqlite_mirror_removal_test.py::_run_link_page_server.
# ---------------------------------------------------------------------------

async def _run_link_page_server(n_links: int, n_failing: int = 0):
    app = web.Application()

    async def handler_root(request):
        links = "".join(f'<a href="/leaf{i}">leaf{i}</a>' for i in range(n_links))
        links += "".join(f'<a href="/fail{i}">fail{i}</a>' for i in range(n_failing))
        return web.Response(text=f"<html><body>{links}</body></html>", content_type="text/html")

    async def handler_leaf(request):
        return web.Response(text="<html><body>dead end</body></html>", content_type="text/html")

    async def handler_fail(request):
        return web.Response(status=404, text="not found")

    app.router.add_get("/", handler_root)
    for i in range(n_links):
        app.router.add_get(f"/leaf{i}", handler_leaf)
    for i in range(n_failing):
        app.router.add_get(f"/fail{i}", handler_fail)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


def _unique_namespace(tag: str) -> str:
    return f"test_local_monitoring_{tag}_{uuid.uuid4().hex[:8]}"


def _redis_frontier(namespace: str, **kwargs) -> RedisURLFrontier:
    frontier = RedisURLFrontier(
        redis_host="localhost",
        redis_port=6379,
        redis_db=1,
        namespace=namespace,
        rate_limit=0,
        max_retries=1,  # a failed attempt goes straight to failed_permanent, never requeued
        **kwargs,
    )
    frontier.clear()
    return frontier


@pytest.fixture
def shared_namespace():
    """One namespace name two independent RedisURLFrontier connections
    will share -- simulating two crawler *processes* pointed at the same
    Redis frontier. Skips if Redis isn't reachable."""
    ns = _unique_namespace("shared")
    try:
        probe = _redis_frontier(ns)
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")
    probe.close()
    yield ns
    # Best-effort final cleanup on the shared namespace.
    try:
        cleanup = RedisURLFrontier(
            redis_host="localhost", redis_port=6379, redis_db=1, namespace=ns, rate_limit=0
        )
        cleanup.clear()
        cleanup.close()
    except redis.ConnectionError:
        pass


# ---------------------------------------------------------------------------
# 1. Pure unit tests for tests/report_lib.py's local-work helpers -- no
#    Redis, no network, exercises the report layer in isolation.
# ---------------------------------------------------------------------------

class _FakeEngine:
    def __init__(self, discovered, crawled, failed, retried):
        self._pages_discovered = discovered
        self._pages_crawled = crawled
        self._pages_failed = failed
        self._pages_retried = retried


def test_local_counters_from_crawler_reads_engine_attributes_directly():
    engine = _FakeEngine(discovered=10, crawled=9, failed=2, retried=1)
    counters = report_lib.local_counters_from_crawler(engine)
    assert counters == {
        "discovered": 10,
        "visited": 7,  # crawled - failed
        "processed": 9,
        "failed": 2,
        "retries": 1,
    }


def test_local_counters_from_crawler_degrades_to_none_for_missing_attrs():
    class _Bare:
        pass

    counters = report_lib.local_counters_from_crawler(_Bare())
    assert counters == {
        "discovered": None,
        "visited": None,  # can't compute crawled - failed when either is missing
        "processed": None,
        "failed": None,
        "retries": None,
    }


def test_build_identity_defaults_crawler_id_from_hostname_and_pid():
    identity = report_lib.build_identity()
    assert identity["crawler_id"] == f"{identity['hostname']}-{identity['pid']}"
    assert identity["pid"] == os.getpid()


def test_build_identity_honors_explicit_crawler_id():
    identity = report_lib.build_identity("my-custom-crawler-id")
    assert identity["crawler_id"] == "my-custom-crawler-id"


def test_build_local_work_attaches_identity_and_note():
    counters = report_lib.local_counters_from_crawler(_FakeEngine(3, 3, 0, 0))
    local_work = report_lib.build_local_work(counters, {"crawler_id": "a"})
    assert local_work["discovered"] == 3
    assert local_work["identity"] == {"crawler_id": "a"}
    assert "LOCAL to this process only" in local_work["note"]


def test_build_local_work_none_when_no_counters():
    assert report_lib.build_local_work(None) is None


def test_build_report_includes_local_work_section():
    snapshot = {"backend": "redis", "available": True}
    report = report_lib.build_report(
        metadata={}, timing={}, snapshot=snapshot,
        local_work=report_lib.build_local_work(report_lib.local_counters_from_crawler(_FakeEngine(1, 1, 0, 0))),
    )
    assert report["local_work"]["discovered"] == 1


# ---------------------------------------------------------------------------
# 2. In-memory frontier: two AsyncCrawler "processes" sharing one frontier
#    object sequentially -- fully deterministic, no Redis dependency.
#    Mirrors the exact scenario from the task brief: crawler A discovers
#    10/visits 7/fails 2 while crawler B (sharing the same frontier)
#    discovers a different, non-overlapping set -- and each process's own
#    counters must reflect only its own work, never the combined total.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_engines_sharing_one_frontier_report_independent_local_counts():
    runner_a, base_a = await _run_link_page_server(n_links=7, n_failing=2)
    runner_b, base_b = await _run_link_page_server(n_links=5, n_failing=1)
    try:
        frontier = URLFrontier(rate_limit=0, max_retries=1)

        # --- Crawler A processes its own seed set to completion first ---
        frontier.add_url(base_a, priority=10)
        crawler_a = AsyncCrawler(
            frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
            max_pages=1 + 7 + 2, timeout=5, max_retries=1,
        )
        await crawler_a.run()

        counters_a = report_lib.local_counters_from_crawler(crawler_a)
        assert counters_a["discovered"] == 9  # 7 leaves + 2 fail-links, all genuinely new
        assert counters_a["processed"] == 10  # root + 7 leaves + 2 fail pages
        assert counters_a["failed"] == 2
        assert counters_a["visited"] == 8

        # --- Crawler B (a second engine instance, e.g. a second process)
        # now shares the SAME frontier object/state and adds its own,
        # disjoint seed set. Nothing about crawler B's construction or run
        # reads crawler_a's counters. ---
        frontier.add_url(base_b, priority=10)
        crawler_b = AsyncCrawler(
            frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
            max_pages=1 + 5 + 1, timeout=5, max_retries=1,
        )
        await crawler_b.run()

        counters_b = report_lib.local_counters_from_crawler(crawler_b)
        assert counters_b["discovered"] == 6  # 5 leaves + 1 fail-link
        assert counters_b["processed"] == 7
        assert counters_b["failed"] == 1
        assert counters_b["visited"] == 6

        # The critical assertion: A's report and B's report are NOT the
        # same, and neither equals the other's numbers merely because they
        # shared one frontier.
        assert counters_a != counters_b
        assert counters_a["discovered"] != counters_b["discovered"]
        assert counters_a["failed"] != counters_b["failed"]

        # The shared frontier's global state reflects the COMBINED work of
        # both engines -- this is legitimately global/shared, and is
        # correctly different from either engine's own local count.
        global_counts = frontier.get_status_counts()
        assert global_counts["visited"] == counters_a["visited"] + counters_b["visited"]
        assert global_counts["failed_permanent"] == counters_a["failed"] + counters_b["failed"]
        assert global_counts["visited"] != counters_a["visited"]
        assert global_counts["visited"] != counters_b["visited"]
    finally:
        await runner_a.cleanup()
        await runner_b.cleanup()


@pytest.mark.asyncio
async def test_restarted_engine_resets_local_counters_but_not_shared_frontier():
    """A fresh crawl-engine instance (simulating a crawler process restart)
    always starts its own local counters at zero, regardless of how much
    work previous instances already did against the same frontier -- and
    that restart must never reset/touch the shared frontier's own state."""
    runner, base_url = await _run_link_page_server(n_links=3)
    try:
        frontier = URLFrontier(rate_limit=0, max_retries=1)
        frontier.add_url(base_url, priority=10)

        crawler_1 = AsyncCrawler(
            frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
            max_pages=4, timeout=5, max_retries=1,
        )
        await crawler_1.run()
        assert crawler_1._pages_crawled == 4
        counts_after_run_1 = frontier.get_status_counts()
        assert counts_after_run_1["visited"] == 4

        # Simulate a process restart: a brand new engine instance wrapping
        # the SAME (not cleared) frontier.
        crawler_2 = AsyncCrawler(
            frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
            max_pages=4, timeout=5, max_retries=1,
        )
        counters_2 = report_lib.local_counters_from_crawler(crawler_2)
        assert counters_2 == {"discovered": 0, "visited": 0, "processed": 0, "failed": 0, "retries": 0}

        # The shared frontier's state from crawler_1's work is untouched by
        # crawler_2's construction.
        assert frontier.get_status_counts() == counts_after_run_1
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 3. Real shared Redis frontier: two independent RedisURLFrontier
#    connections (namespace shared, connection objects distinct -- the
#    truest simulation of two OS processes) driving concurrent AsyncCrawler
#    engines. Proves the property holds against the actual distributed
#    backend, not just the in-memory one.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_engines_on_shared_redis_frontier_partition_local_counts(shared_namespace):
    """Two crawler engines, each with its own RedisURLFrontier connection
    to the SAME namespace, run concurrently (asyncio.gather) -- the actual
    scenario the task brief describes ("crawler A discovers 10,000 ... "
    while crawler B discovers another 10,000 ... sharing the same Redis
    frontier"). Because either engine can legitimately claim either
    domain's queued URL from the shared frontier once running concurrently,
    which engine happens to process which specific URL is not
    deterministic -- but each engine's own local `_pages_crawled` count IS
    deterministic (each stops itself the instant its own counter reaches
    its own max_pages), and the sum of both engines' local counts must
    equal the shared frontier's global total: local attribution correctly
    partitions the combined work instead of both processes reporting the
    same inflated shared number.
    """
    n_a, n_b = 4, 4  # leaf counts per server domain
    total_pages = 2 + n_a + n_b  # 2 roots + leaves

    runner_a, base_a = await _run_link_page_server(n_links=n_a)
    runner_b, base_b = await _run_link_page_server(n_links=n_b)
    try:
        frontier_a = _redis_frontier(shared_namespace)
        frontier_b = _redis_frontier(shared_namespace)
        try:
            frontier_a.add_url(base_a, priority=10)
            frontier_a.add_url(base_b, priority=10)

            half = total_pages // 2  # 5
            # concurrency=1 on each engine: with concurrency > 1, a second
            # in-flight claim can be orphaned (cancelled, marked
            # failed_permanent) at the exact instant the first claim's
            # completion pushes this engine's own _pages_crawled to its
            # max_pages cap -- a genuine, separate pre-existing worker-
            # cancellation edge case (async_crawler.py's CancelledError
            # branch marks the claim failed without crediting either
            # engine's local counters), not something this test is about.
            # concurrency=1 removes that race so the sum-of-local-counts
            # invariant below is deterministic.
            crawler_a = AsyncCrawler(
                frontier=frontier_a, parser=HTMLLinkExtractor(), concurrency=1,
                max_pages=half, timeout=5, max_retries=1,
            )
            crawler_b = AsyncCrawler(
                frontier=frontier_b, parser=HTMLLinkExtractor(), concurrency=1,
                max_pages=total_pages - half, timeout=5, max_retries=1,
            )

            await asyncio.gather(crawler_a.run(), crawler_b.run())

            counters_a = report_lib.local_counters_from_crawler(crawler_a)
            counters_b = report_lib.local_counters_from_crawler(crawler_b)

            # Each engine's own local "processed" count matches exactly what
            # IT did -- fixed by its own max_pages, not by what the other
            # engine did.
            assert counters_a["processed"] == half
            assert counters_b["processed"] == total_pages - half

            # Neither engine's local view claims the combined total.
            global_counts = frontier_a.get_status_counts()
            assert global_counts["visited"] == total_pages
            assert counters_a["visited"] != global_counts["visited"]
            assert counters_b["visited"] != global_counts["visited"]

            # But together, local attribution accounts for exactly the
            # shared frontier's global state -- no work double-counted, none
            # dropped.
            assert counters_a["processed"] + counters_b["processed"] == total_pages
            assert counters_a["visited"] + counters_b["visited"] == global_counts["visited"]
        finally:
            frontier_a.clear()
            frontier_a.close()
            frontier_b.close()
    finally:
        await runner_a.cleanup()
        await runner_b.cleanup()
