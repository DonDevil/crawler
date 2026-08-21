"""Tests for core/target_scope.py -- the Phase 3 target-registration/scoping
unblocker (docs/architecture/phase-3-target-registration-and-scoping.md).

Covers: scope resolution from config/CLI-shaped input (missing-field
failures, no-scope-configured, no-fabrication), and read-only existence
validation against the fingerprinter's own `fingerprint:target:*` key
convention. The Redis-backed tests need a live Redis on localhost:6379 --
skip if unavailable, matching every other Redis-backed test's convention in
this repo.
"""

from __future__ import annotations

import pytest
import redis

from core.target_scope import TargetScope, TargetScopeError, resolve_target_scope, verify_target_registered


@pytest.fixture
def redis_conn():
    """A raw Redis connection into the crawler's Redis-test DB (1) -- not a
    `RedisMediaEvidenceStore`, since these tests seed/read the
    fingerprinter's own `fingerprint:target:*` keys directly, entirely
    outside the `evidence:*` namespace any evidence store instance owns."""
    try:
        conn = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        conn.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")

    keys = conn.keys("fingerprint:target:target_scope_test:*")
    if keys:
        conn.delete(*keys)
    yield conn
    keys = conn.keys("fingerprint:target:target_scope_test:*")
    if keys:
        conn.delete(*keys)


class TestResolveTargetScope:
    def test_no_scope_configured_returns_none(self):
        """No target_id/target_version at all -- today's default, unscoped
        behavior. Never a fabricated default target (brief: "Do not create
        an implicit default target")."""
        assert resolve_target_scope(None, None) is None
        assert resolve_target_scope("", "") is None

    def test_missing_target_version_fails_clearly(self):
        with pytest.raises(TargetScopeError):
            resolve_target_scope("target_scope_test:blast", None)

    def test_missing_target_id_fails_clearly(self):
        with pytest.raises(TargetScopeError):
            resolve_target_scope(None, "v1")

    def test_both_present_returns_a_scope(self):
        scope = resolve_target_scope("target_scope_test:blast", "v1")
        assert scope == TargetScope(target_id="target_scope_test:blast", target_version="v1")

    def test_scope_is_never_inferred_from_a_query_or_title_string(self):
        """`resolve_target_scope` only ever accepts explicit target_id/
        target_version values -- it has no code path that reads a search
        query, filename, or discovered title. Passing a query-shaped string
        through the *correct* parameter still works, because identity here
        is whatever the caller explicitly names, never something this
        function derives on its own (brief: "Do not infer target scope
        from search queries")."""
        scope = resolve_target_scope("Blast full movie download", "v1")
        assert scope is not None
        assert scope.target_id == "Blast full movie download"


class TestTargetScopeValueObject:
    def test_rejects_empty_target_id(self):
        with pytest.raises(TargetScopeError):
            TargetScope(target_id="", target_version="v1")

    def test_rejects_empty_target_version(self):
        with pytest.raises(TargetScopeError):
            TargetScope(target_id="target_scope_test:blast", target_version="")


class TestVerifyTargetRegistered:
    def test_registered_target_is_selectable(self, redis_conn):
        redis_conn.hset(
            "fingerprint:target:target_scope_test:blast:v1",
            mapping={"target_id": "target_scope_test:blast", "target_version": "v1"},
        )
        scope = TargetScope(target_id="target_scope_test:blast", target_version="v1")
        assert verify_target_registered(redis_conn, scope) is True

    def test_unregistered_target_fails_clearly(self, redis_conn):
        scope = TargetScope(target_id="target_scope_test:unregistered", target_version="v1")
        assert verify_target_registered(redis_conn, scope) is False

    def test_multiple_targets_are_selectable_without_ambiguity(self, redis_conn):
        redis_conn.hset(
            "fingerprint:target:target_scope_test:blast:v1",
            mapping={"target_id": "target_scope_test:blast", "target_version": "v1"},
        )
        redis_conn.hset(
            "fingerprint:target:target_scope_test:otherfilm:v2",
            mapping={"target_id": "target_scope_test:otherfilm", "target_version": "v2"},
        )
        blast = TargetScope(target_id="target_scope_test:blast", target_version="v1")
        other = TargetScope(target_id="target_scope_test:otherfilm", target_version="v2")
        mismatched = TargetScope(target_id="target_scope_test:blast", target_version="v2")

        assert verify_target_registered(redis_conn, blast) is True
        assert verify_target_registered(redis_conn, other) is True
        # A registered target_id with an unregistered target_version is not
        # silently accepted -- target_version is part of identity, not a
        # detail (brief: "target_version must represent the registered
        # reference-media version").
        assert verify_target_registered(redis_conn, mismatched) is False
