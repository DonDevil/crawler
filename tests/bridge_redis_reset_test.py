"""Tests for bridge/redis_reset.py -- the SCAN+DELETE helper behind
`bridge.main --clear-db` / `bridge.result_consumer_main --clear-db`. Run
against real local Redis (test DB 1, this repo's existing Redis-test
convention, see bridge_test.py) -- skip cleanly if unavailable. Never
touches production DB 0.
"""
from __future__ import annotations

import pytest
import redis

from bridge.redis_reset import clear_fingerprint_namespace, clear_namespace


@pytest.fixture
def conn():
    try:
        client = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")

    for key in client.keys("test_reset:*") + client.keys("fingerprint:*"):
        client.delete(key)
    yield client
    for key in client.keys("test_reset:*") + client.keys("fingerprint:*"):
        client.delete(key)
    client.close()


def test_clear_namespace_deletes_only_matching_prefix(conn):
    conn.set("test_reset:a", "1")
    conn.set("test_reset:b", "1")
    conn.set("other:c", "1")

    deleted = clear_namespace(conn, "test_reset")

    assert deleted == 2
    assert conn.exists("test_reset:a") == 0
    assert conn.exists("test_reset:b") == 0
    assert conn.exists("other:c") == 1
    conn.delete("other:c")


def test_clear_namespace_respects_exclude_prefixes(conn):
    conn.set("test_reset:keep:1", "1")
    conn.set("test_reset:drop:1", "1")

    deleted = clear_namespace(conn, "test_reset", exclude_prefixes=("test_reset:keep:",))

    assert deleted == 1
    assert conn.exists("test_reset:keep:1") == 1
    assert conn.exists("test_reset:drop:1") == 0
    conn.delete("test_reset:keep:1")


def test_clear_fingerprint_namespace_preserves_targets_and_locks_by_default(conn):
    conn.set("fingerprint:job:job-1:state", "1")
    conn.set("fingerprint:results:stream:default", "1")
    conn.set("fingerprint:target:t1:v1", "1")
    conn.set("fingerprint:lock:target-record:t1:v1", "1")

    deleted = clear_fingerprint_namespace(conn)

    assert deleted == 2
    assert conn.exists("fingerprint:job:job-1:state") == 0
    assert conn.exists("fingerprint:results:stream:default") == 0
    assert conn.exists("fingerprint:target:t1:v1") == 1
    assert conn.exists("fingerprint:lock:target-record:t1:v1") == 1


def test_clear_fingerprint_namespace_include_targets_wipes_everything(conn):
    conn.set("fingerprint:target:t1:v1", "1")
    conn.set("fingerprint:lock:target-record:t1:v1", "1")

    deleted = clear_fingerprint_namespace(conn, include_targets=True)

    assert deleted == 2
    assert conn.exists("fingerprint:target:t1:v1") == 0
    assert conn.exists("fingerprint:lock:target-record:t1:v1") == 0
