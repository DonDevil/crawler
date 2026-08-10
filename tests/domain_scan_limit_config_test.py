"""Regression coverage for the domain_scan_limit (K) configuration default.

See docs/architecture/domain-scan-limit-decision.md: the default was raised
from 50 to 250 through config.yaml / core/config.py's FrontierConfig. These
tests exist to catch two distinct regressions:

1. The default drifting back down (or apart between the config.yaml value,
   the FrontierConfig pydantic default, and the RedisURLFrontier constructor
   default -- three places that must stay in sync, see the decision doc).
2. The configured value silently failing to reach the actual RedisURLFrontier
   instance CrawlerManager builds (a wiring bug, not just a wrong constant).

Redis-dependent tests run ONLY if Redis is available locally on port 6379,
matching tests/redis_frontier_test.py's convention.
"""

from __future__ import annotations

import inspect

import pytest
import redis

from core.config import Config, CrawlerConfig, FrontierConfig, SearchConfig, StorageConfig, load_config
from core.crawler_manager import CrawlerManager
from core.redis_frontier import RedisURLFrontier


def test_frontier_config_default_domain_scan_limit_is_250():
    """The pydantic default (used when config.yaml doesn't set it) must
    match the production config.yaml value -- both are "the configuration
    mechanism" per the decision doc and must never silently diverge."""
    assert FrontierConfig().domain_scan_limit == 250


def test_redis_url_frontier_constructor_default_matches_config_default():
    """RedisURLFrontier's own constructor default mirrors FrontierConfig's
    (the existing convention already followed by rate_limit/max_retries/
    lease_ttl/reclaim_batch_size) -- checked without opening a Redis
    connection, since only the signature matters here."""
    default = inspect.signature(RedisURLFrontier.__init__).parameters["domain_scan_limit"].default
    assert default == FrontierConfig().domain_scan_limit == 250


def test_config_yaml_sets_domain_scan_limit_to_250():
    config = load_config("config.yaml")
    assert config.crawler.frontier.domain_scan_limit == 250


def _redis_config(tmp_path, *, domain_scan_limit: int | None, namespace: str) -> Config:
    frontier_kwargs = dict(
        type="redis",
        redis_host="localhost",
        redis_port=6379,
        redis_db=1,  # matches tests/redis_frontier_test.py's shared test DB
        redis_namespace=namespace,
    )
    if domain_scan_limit is not None:
        frontier_kwargs["domain_scan_limit"] = domain_scan_limit

    return Config(
        crawler=CrawlerConfig(
            storage=StorageConfig(sqlite_path=str(tmp_path / "crawl.db")),
            max_pages=1,
            frontier=FrontierConfig(**frontier_kwargs),
        ),
        search=SearchConfig(enabled_engines=["duckduckgo"], engine_priorities={"duckduckgo": 6}),
    )


def _build_manager_or_skip(config: Config) -> CrawlerManager:
    try:
        manager = CrawlerManager(config=config)
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")
    if not isinstance(manager.frontier, RedisURLFrontier):
        pytest.skip("Redis not available on localhost:6379 (manager fell back to local frontier)")
    return manager


def test_crawler_manager_propagates_configured_domain_scan_limit_to_redis_frontier(tmp_path):
    """An explicit override in config must reach the live RedisURLFrontier
    instance CrawlerManager builds -- not just be readable off the Config
    object."""
    config = _redis_config(tmp_path, domain_scan_limit=77, namespace="test_scan_limit_cfg_override")
    manager = _build_manager_or_skip(config)
    try:
        assert manager.frontier.domain_scan_limit == 77
    finally:
        manager.frontier.close()
        manager.url_database.close()
        manager.domain_database.close()


def test_crawler_manager_uses_default_domain_scan_limit_when_unset(tmp_path):
    """With no explicit override, the live frontier must end up at the
    FrontierConfig default (250), proving the default itself -- not just an
    explicit override -- actually propagates end to end."""
    config = _redis_config(tmp_path, domain_scan_limit=None, namespace="test_scan_limit_cfg_default")
    manager = _build_manager_or_skip(config)
    try:
        assert manager.frontier.domain_scan_limit == 250
    finally:
        manager.frontier.close()
        manager.url_database.close()
        manager.domain_database.close()
