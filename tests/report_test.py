"""Tests for tests/report_lib.py -- the reporting layer that reads the
current Redis frontier keyspace (core/redis_frontier.py) and the SQLite
crawl-state database (storage/url_database.py) and produces the crawl-run
report (tests/report.py).

Redis-backed tests use db=1 (matches tests/redis_frontier_test.py's
convention) and a per-test unique namespace so runs never collide, and are
skipped if Redis isn't reachable on localhost:6379.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report_lib  # noqa: E402
from core.redis_frontier import RedisURLFrontier  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _unique_namespace(tag: str) -> str:
    return f"test_report_{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def make_frontier():
    """Factory fixture: build a fresh, isolated RedisURLFrontier per call,
    tracked for cleanup. Skips the whole test if Redis is unreachable."""
    created: list[RedisURLFrontier] = []

    def _make(**kwargs) -> RedisURLFrontier:
        try:
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace=_unique_namespace("state"),
                rate_limit=0,
                **kwargs,
            )
            frontier.clear()
        except redis.ConnectionError:
            pytest.skip("Redis not available on localhost:6379")
        created.append(frontier)
        return frontier

    yield _make

    for frontier in created:
        frontier.clear()
        frontier.close()


@pytest.fixture
def sqlite_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def _write_sqlite_rows(path: str, rows: list[tuple[str, str, str, str]]) -> None:
    """rows: (url, first_seen, last_seen, status)"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls (url TEXT PRIMARY KEY, first_seen TIMESTAMP, last_seen TIMESTAMP, status TEXT)"
    )
    conn.executemany("INSERT INTO urls VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Known synthetic Redis frontier state -> exact counts
# ---------------------------------------------------------------------------

def test_redis_report_known_synthetic_state(make_frontier):
    frontier = make_frontier(max_retries=3)
    namespace = frontier.namespace

    # Synthetic workload: 3 URLs that get visited, 1 that gets skipped.
    urls_to_visit = [f"https://visit{i}.example.test/" for i in range(3)]
    url_to_skip = "https://skip0.example.test/"
    for url in urls_to_visit + [url_to_skip]:
        frontier.add_url(url, priority=5)

    claims_by_url = {}
    for _ in range(len(urls_to_visit) + 1):
        claim = frontier.get_next_url()
        claims_by_url[claim.url] = claim

    for url in urls_to_visit:
        frontier.mark_visited(claims_by_url[url])
    frontier.mark_skipped(claims_by_url[url_to_skip])

    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, namespace)

    assert snapshot["available"] is True
    assert snapshot["discovered_total"] == 4
    assert snapshot["visited"] == 3
    assert snapshot["skipped"] == 1
    assert snapshot["queued"] == 0
    assert snapshot["inflight"] == 0
    assert snapshot["retry_scheduled"] == 0
    assert snapshot["failed_permanent"] == 0


# ---------------------------------------------------------------------------
# 2. Empty frontier
# ---------------------------------------------------------------------------

def test_redis_report_empty_frontier(make_frontier):
    frontier = make_frontier()
    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, frontier.namespace)

    assert snapshot["available"] is True
    for key in ("discovered_total", "visited", "queued", "inflight", "retry_scheduled", "failed_permanent", "skipped"):
        assert snapshot[key] == 0

    report = report_lib.build_report(
        metadata={"backend": "redis", "worker_count": None},
        timing=report_lib.redis_timing_unavailable(frontier.namespace),
        snapshot=snapshot,
    )
    # Zero is a legitimate observed value here (an empty frontier), not an
    # unavailable metric -- percentages against a zero total must be N/A,
    # not a fabricated 0%/NaN.
    assert report["counts"]["percentages"]["visited_pct"] is None


# ---------------------------------------------------------------------------
# 3. Queued URLs
# ---------------------------------------------------------------------------

def test_redis_report_queued_urls(make_frontier):
    frontier = make_frontier()
    for i in range(5):
        frontier.add_url(f"https://q{i}.example.test/", priority=5)

    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, frontier.namespace)
    assert snapshot["queued"] == 5
    assert snapshot["discovered_total"] == 5
    assert snapshot["visited"] == 0


# ---------------------------------------------------------------------------
# 4. Visited URLs
# ---------------------------------------------------------------------------

def test_redis_report_visited_urls(make_frontier):
    frontier = make_frontier()
    frontier.add_url("https://a.example.test/", priority=5)
    frontier.add_url("https://b.example.test/", priority=5)

    for _ in range(2):
        claim = frontier.get_next_url()
        frontier.mark_visited(claim)

    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, frontier.namespace)
    assert snapshot["visited"] == 2
    assert snapshot["queued"] == 0


# ---------------------------------------------------------------------------
# 5. Permanently failed URLs
# ---------------------------------------------------------------------------

def test_redis_report_permanently_failed_urls(make_frontier):
    # max_retries=1 -> the first failed attempt (attempt=1) is already >= max_retries,
    # so it lands directly in failed_permanent.
    frontier = make_frontier(max_retries=1)
    frontier.add_url("https://fails.example.test/", priority=5)

    claim = frontier.get_next_url()
    frontier.mark_failed(claim, "simulated failure")

    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, frontier.namespace)
    assert snapshot["failed_permanent"] == 1
    assert snapshot["retry_scheduled"] == 0
    assert snapshot["queued"] == 0


# ---------------------------------------------------------------------------
# 6. Retry-scheduled and in-flight state
# ---------------------------------------------------------------------------

def test_redis_report_retry_and_inflight_state(make_frontier):
    frontier = make_frontier(max_retries=3, base_backoff=1000, max_backoff=1000)

    frontier.add_url("https://retry.example.test/", priority=5)
    frontier.add_url("https://inflight.example.test/", priority=5)

    retry_claim = frontier.get_next_url()
    inflight_claim = frontier.get_next_url()
    assert {retry_claim.url, inflight_claim.url} == {
        "https://retry.example.test/",
        "https://inflight.example.test/",
    }

    frontier.mark_failed(retry_claim, "transient")
    # inflight_claim intentionally left uncompleted.

    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, frontier.namespace)
    assert snapshot["retry_scheduled"] == 1
    assert snapshot["inflight"] == 1
    assert snapshot["queued"] == 0
    assert snapshot["discovered_total"] == 2


# ---------------------------------------------------------------------------
# 7. No obsolete Redis keys required
# ---------------------------------------------------------------------------

def test_redis_report_does_not_require_obsolete_keys(make_frontier):
    frontier = make_frontier()
    frontier.add_url("https://x.example.test/", priority=5)
    claim = frontier.get_next_url()
    frontier.mark_visited(claim)

    conn = frontier.redis_conn
    namespace = frontier.namespace
    # The previous report's assumed keys must never exist in the current keyspace.
    assert conn.exists(f"{namespace}:urls:queued") == 0
    assert conn.exists(f"{namespace}:urls:failed") == 0

    snapshot = report_lib.redis_snapshot("localhost", 6379, 1, namespace)
    assert snapshot["available"] is True
    assert snapshot["visited"] == 1


# ---------------------------------------------------------------------------
# 8. Missing metric reports N/A, never 0
# ---------------------------------------------------------------------------

def test_redis_unreachable_reports_unavailable_not_zero():
    # Port 1 refuses connections on any normal host -- a fast, reliable way
    # to simulate "Redis is down" without a timeout-based sleep.
    snapshot = report_lib.redis_snapshot("localhost", 1, 1, "unreachable_ns")
    assert snapshot["available"] is False
    assert "error" in snapshot
    # An infrastructure failure must never be reported as a legitimate all-zero state.
    for key in ("discovered_total", "visited", "queued", "inflight", "retry_scheduled", "failed_permanent", "skipped"):
        assert key not in snapshot


def test_redis_timing_unavailable_is_none_not_zero():
    timing = report_lib.redis_timing_unavailable("some_namespace")
    assert timing["start"] is None
    assert timing["end"] is None
    assert timing["duration_seconds"] is None
    assert timing["source"] == "unavailable"
    assert "note" in timing


# ---------------------------------------------------------------------------
# 9. SQL report still works
# ---------------------------------------------------------------------------

def test_sqlite_report_still_works(sqlite_db_path):
    t0 = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=30)

    _write_sqlite_rows(
        sqlite_db_path,
        [
            ("https://v1.example.test/", t0.isoformat(), t0.isoformat(), "visited"),
            ("https://v2.example.test/", t0.isoformat(), t1.isoformat(), "visited"),
            ("https://f1.example.test/", t0.isoformat(), t1.isoformat(), "failed"),
            ("https://q1.example.test/", t0.isoformat(), t0.isoformat(), "queued"),
            ("https://p1.example.test/", t0.isoformat(), t0.isoformat(), "pending"),
        ],
    )

    snapshot = report_lib.sqlite_snapshot(sqlite_db_path)
    assert snapshot["backend"] == "sqlite"
    assert snapshot["discovered_total"] == 5
    assert snapshot["visited"] == 2
    assert snapshot["failed_permanent"] == 1
    assert snapshot["queued"] == 1
    assert snapshot["inflight"] == 1
    assert snapshot["retry_scheduled"] is None

    timing = report_lib.sqlite_timing(snapshot)
    assert timing["source"] == "sqlite-first-last-seen"
    assert timing["duration_seconds"] == pytest.approx(1800.0)

    report = report_lib.build_report(
        metadata={"backend": "sqlite", "worker_count": None}, timing=timing, snapshot=snapshot
    )
    assert report["counts"]["percentages"]["visited_pct"] == pytest.approx(40.0)
    assert report["throughput"]["visited_per_sec"] == pytest.approx(2 / 1800.0)


# ---------------------------------------------------------------------------
# 10. Throughput calculations
# ---------------------------------------------------------------------------

def test_throughput_calculation():
    snapshot = {
        "discovered_total": 1000,
        "visited": 500,
        "failed_permanent": 50,
        "skipped": 10,
    }
    throughput = report_lib.compute_throughput(snapshot, duration_seconds=100.0, worker_count=5)

    assert throughput["discovered_per_sec"] == pytest.approx(10.0)
    assert throughput["visited_per_sec"] == pytest.approx(5.0)
    assert throughput["completed_per_sec"] == pytest.approx(5.6)  # (500+50+10)/100
    assert throughput["avg_completed_per_sec_per_worker"] == pytest.approx(5.6 / 5)
    assert throughput["note"] is None


# ---------------------------------------------------------------------------
# 11. Zero-duration protection
# ---------------------------------------------------------------------------

def test_throughput_zero_duration_protection():
    snapshot = {"discovered_total": 100, "visited": 50, "failed_permanent": 0, "skipped": 0}

    for bad_duration in (0.0, -5.0, None):
        throughput = report_lib.compute_throughput(snapshot, duration_seconds=bad_duration)
        assert throughput["discovered_per_sec"] is None
        assert throughput["visited_per_sec"] is None
        assert throughput["completed_per_sec"] is None
        assert throughput["note"] is not None


def test_safe_rate_and_safe_pct_guard_zero_and_none():
    assert report_lib.safe_rate(10, 0) is None
    assert report_lib.safe_rate(10, -1) is None
    assert report_lib.safe_rate(10, None) is None
    assert report_lib.safe_rate(None, 10) is None
    assert report_lib.safe_rate(10, 5) == 2.0

    assert report_lib.safe_pct(5, 0) is None
    assert report_lib.safe_pct(None, 10) is None
    assert report_lib.safe_pct(5, 10) == 50.0


# ---------------------------------------------------------------------------
# 12. Resource result schema
# ---------------------------------------------------------------------------

def test_resource_report_schema():
    samples = [
        {"t": 1.0, "process_cpu_percent": 10.0, "process_rss_mb": 100.0},
        {"t": 2.0, "process_cpu_percent": 30.0, "process_rss_mb": 120.0},
        {"t": 3.0, "process_cpu_percent": 20.0, "process_rss_mb": 110.0},
    ]
    resources = report_lib.build_resource_report(samples, interval=1.0)

    assert resources["sample_count"] == 3
    assert resources["sample_interval_seconds"] == 1.0
    assert resources["process_cpu_percent_avg"] == pytest.approx(20.0)
    assert resources["process_cpu_percent_peak"] == pytest.approx(30.0)
    assert resources["process_rss_mb_avg"] == pytest.approx(110.0)
    assert resources["process_rss_mb_peak"] == pytest.approx(120.0)

    assert report_lib.build_resource_report([], interval=1.0) is None


def test_redis_resource_report_schema_and_cpu_time_semantics():
    samples = [
        {"t": 1.0, "redis_used_memory_mb": 50.0, "redis_connected_clients": 3,
         "redis_used_cpu_sys": 10.0, "redis_used_cpu_user": 5.0},
        {"t": 2.0, "redis_used_memory_mb": 80.0, "redis_connected_clients": 5,
         "redis_used_cpu_sys": 11.5, "redis_used_cpu_user": 5.8},
    ]
    redis_resources = report_lib.build_redis_resource_report(samples, interval=1.0)

    assert redis_resources["memory_start_mb"] == 50.0
    assert redis_resources["memory_end_mb"] == 80.0
    assert redis_resources["memory_delta_mb"] == pytest.approx(30.0)
    assert redis_resources["memory_peak_sampled_mb"] == 80.0
    assert redis_resources["connected_clients_avg"] == pytest.approx(4.0)
    assert redis_resources["connected_clients_peak"] == 5
    # Deltas of cumulative CPU-TIME counters (seconds), not a percentage.
    assert redis_resources["cpu_sys_seconds_delta"] == pytest.approx(1.5)
    assert redis_resources["cpu_user_seconds_delta"] == pytest.approx(0.8)
    assert "CPU-TIME" in redis_resources["note"]

    assert report_lib.build_redis_resource_report([], interval=1.0) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
