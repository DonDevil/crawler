"""Tests for CrawlerManager's target-scope resolution/validation at
construction time (docs/architecture/phase-3-target-registration-and-scoping.md).

Redis-backed -- skips if Redis is unavailable on localhost:6379, matching
every other Redis-backed test's convention in this repo. Uses DB 1 (the
crawler's own Redis test-isolation convention) for both the evidence
namespace and the fingerprinter's `fingerprint:target:*` keys this phase
reads (read-only, never DB 0/production).
"""

from __future__ import annotations

import pytest
import redis

from core.config import Config, CrawlerConfig, FrontierConfig, MediaEvidenceConfig, SearchConfig, StorageConfig
from core.crawler_manager import CrawlerManager
from core.target_scope import TargetNotRegisteredError, TargetScopeError

_TEST_REDIS_DB = 1


def _redis_available() -> bool:
    try:
        redis.Redis(host="localhost", port=6379, db=_TEST_REDIS_DB).ping()
        return True
    except redis.ConnectionError:
        return False


def _make_config(tmp_path, namespace: str, target_id=None, target_version=None, media_type="redis") -> Config:
    return Config(
        crawler=CrawlerConfig(
            storage=StorageConfig(sqlite_path=str(tmp_path / "crawl.db")),
            frontier=FrontierConfig(type="sqlite"),
            media_evidence=MediaEvidenceConfig(
                type=media_type,
                redis_host="localhost",
                redis_port=6379,
                redis_db=_TEST_REDIS_DB,
                redis_namespace=namespace,
                target_id=target_id,
                target_version=target_version,
            ),
        ),
        search=SearchConfig(enabled_engines=[]),
    )


def _register_target_key(target_id: str, target_version: str) -> None:
    """Seed a raw `fingerprint:target:*` key, standing in for a real
    `TargetRegistry.register_target()` call from the sibling fingerprinter
    repo (whose full registration machinery is out of this repo's scope --
    see core/target_scope.py's docstring)."""
    conn = redis.Redis(host="localhost", port=6379, db=_TEST_REDIS_DB, decode_responses=True)
    conn.hset(
        f"fingerprint:target:{target_id}:{target_version}",
        mapping={"target_id": target_id, "target_version": target_version},
    )


def _cleanup_target_key(target_id: str, target_version: str) -> None:
    conn = redis.Redis(host="localhost", port=6379, db=_TEST_REDIS_DB, decode_responses=True)
    conn.delete(f"fingerprint:target:{target_id}:{target_version}")


@pytest.fixture(autouse=True)
def _skip_if_redis_unavailable():
    if not _redis_available():
        pytest.skip("Redis not available on localhost:6379")
    yield


class TestTargetScopeValidationAtConstruction:
    def test_missing_target_version_fails_clearly(self, tmp_path):
        config = _make_config(tmp_path, "test_cm_target_scope_a", target_id="blast")

        with pytest.raises(TargetScopeError):
            CrawlerManager(config=config, crawl_engine="http", include_seed_files=False)

    def test_missing_target_id_fails_clearly(self, tmp_path):
        config = _make_config(tmp_path, "test_cm_target_scope_b", target_version="v1")

        with pytest.raises(TargetScopeError):
            CrawlerManager(config=config, crawl_engine="http", include_seed_files=False)

    def test_unregistered_target_fails_clearly(self, tmp_path):
        config = _make_config(
            tmp_path, "test_cm_target_scope_c", target_id="test_cm_unregistered", target_version="v1"
        )

        with pytest.raises(TargetNotRegisteredError):
            CrawlerManager(config=config, crawl_engine="http", include_seed_files=False)

    def test_target_scope_against_sqlite_backend_fails_clearly(self, tmp_path):
        config = _make_config(
            tmp_path, "unused", target_id="blast", target_version="v1", media_type="sqlite"
        )

        with pytest.raises(TargetScopeError):
            CrawlerManager(config=config, crawl_engine="http", include_seed_files=False)

    def test_registered_target_can_be_associated_with_a_crawler_run(self, tmp_path):
        _register_target_key("test_cm_blast", "v1")
        try:
            config = _make_config(
                tmp_path, "test_cm_target_scope_d", target_id="test_cm_blast", target_version="v1"
            )

            manager = CrawlerManager(config=config, crawl_engine="http", include_seed_files=False)
            try:
                assert manager.media_database.target_scope.target_id == "test_cm_blast"
                assert manager.media_database.target_scope.target_version == "v1"
            finally:
                manager.media_database.clear()
                manager.url_database.close()
                manager.domain_database.close()
                manager.media_database.close()
        finally:
            _cleanup_target_key("test_cm_blast", "v1")

    def test_cli_level_target_override_is_validated_too(self, tmp_path):
        """`--target-id`/`--target-version` (main.py) reach `CrawlerManager`
        as constructor overrides -- these must be validated identically to
        a config.yaml-configured scope."""
        config = _make_config(tmp_path, "test_cm_target_scope_e")  # no scope in config.yaml

        with pytest.raises(TargetNotRegisteredError):
            CrawlerManager(
                config=config,
                crawl_engine="http",
                include_seed_files=False,
                target_id="test_cm_cli_override_unregistered",
                target_version="v1",
            )

    def test_no_target_scope_is_unaffected(self, tmp_path):
        """Existing (pre-Phase-3) behavior: a crawler run with no target
        scope configured constructs and runs exactly as before."""
        config = _make_config(tmp_path, "test_cm_target_scope_f")

        manager = CrawlerManager(config=config, crawl_engine="http", include_seed_files=False)
        try:
            assert manager.media_database.target_scope is None
        finally:
            manager.media_database.clear()
            manager.url_database.close()
            manager.domain_database.close()
            manager.media_database.close()
