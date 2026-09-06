"""Tests for crawler manager startup modes."""

import asyncio

import pytest
from aiohttp import web
from discovery.search_engine_discovery import DiscoveryBatchReport, QueryDiscoveryReport

from core.config import Config, CrawlerConfig, SearchConfig, StorageConfig, load_config
from core.crawler_manager import CrawlerManager
from crawler.http_crawler import HTTPCrawler
from crawler.hybrid_crawler import HybridCrawler
from crawler.scrapling_crawler import ScraplingCrawler
from storage.domain_database import DomainDatabase
from utils.url_utils import URLUtils


def _make_config(seed_file: str, sqlite_path: str) -> Config:
    return Config(
        crawler=CrawlerConfig(
            seed_files=[seed_file],
            storage=StorageConfig(sqlite_path=sqlite_path),
            max_pages=1,
        ),
        search=SearchConfig(
            enabled_engines=["duckduckgo"],
            max_results_per_engine=5,
            engine_priorities={"duckduckgo": 6, "torch": 0},
            onion_priority_boost=2,
        ),
    )


def test_load_config_resolves_relative_storage_paths_from_config_dir(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "config.yaml").write_text(
        """
crawler:
  storage:
    sqlite_path: "storage/crawl_state.db"
    media_sqlite_path: "storage/media_evidence.db"
    enable_media_evidence: true
""",
        encoding="utf-8",
    )

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    config = load_config(str(project_dir / "config.yaml"))

    assert config.crawler.storage.sqlite_path == str((project_dir / "storage" / "crawl_state.db").resolve())
    assert config.crawler.storage.media_sqlite_path == str((project_dir / "storage" / "media_evidence.db").resolve())


def test_domain_database_can_be_cleared(tmp_path):
    db_path = tmp_path / "crawl.db"
    database = DomainDatabase(path=str(db_path))

    try:
        database.add_or_update("example.com", score=1.5)
        database.clear()
        assert list(database.list_domains()) == []
    finally:
        database.close()


def test_prepare_frontier_query_only_skips_seed_files(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    report = DiscoveryBatchReport(
        urls=["https://query.example.com"],
        query_reports=[QueryDiscoveryReport(query="movie", urls=["https://query.example.com"])],
    )
    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: report,
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        queries=["movie"],
        include_seed_files=False,
    )

    manager.prepare_frontier()

    claim = manager.frontier.get_next_url()
    assert claim is not None
    assert claim.url == "https://query.example.com/"
    assert manager.frontier.get_next_url() is None


def test_prepare_frontier_unfinished_loads_only_resume_urls(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: DiscoveryBatchReport(
            urls=["https://query.example.com"],
            query_reports=[QueryDiscoveryReport(query="movie", urls=["https://query.example.com"])],
        ),
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        queries=["movie"],
        resume_unfinished=True,
    )
    manager.url_database.add_url("https://queued.example.com", status="queued")
    manager.url_database.add_url("https://pending.example.com", status="pending")
    manager.url_database.add_url("https://visited.example.com", status="visited")

    manager.prepare_frontier()

    resumed = {manager.frontier.get_next_url().url, manager.frontier.get_next_url().url}
    assert resumed == {"https://queued.example.com/", "https://pending.example.com/"}
    assert manager.frontier.get_next_url() is None


def test_prepare_frontier_prioritizes_onion_resume_urls(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: DiscoveryBatchReport(),
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        resume_unfinished=True,
    )
    manager.url_database.add_url("https://surface.example.com", status="pending")
    manager.url_database.add_url("http://resumehiddenresumehidden.onion", status="pending")

    manager.prepare_frontier()

    claim = manager.frontier.get_next_url()
    assert claim is not None
    assert claim.url == "http://resumehiddenresumehidden.onion/"


def test_prepare_frontier_uses_surface_scope_for_queries(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"
    captured = {}

    def _fake_discovery(*args, **kwargs):
        captured["engine_names"] = kwargs.get("engine_names")
        return DiscoveryBatchReport()

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        _fake_discovery,
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        queries=["movie"],
        include_seed_files=False,
        query_scope="surface-web",
    )

    manager.prepare_frontier()

    assert captured["engine_names"] == ["duckduckgo"]


def test_manager_uses_selected_crawler_engine(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: DiscoveryBatchReport(),
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        crawl_engine="http",
    )

    assert isinstance(manager._crawler, HTTPCrawler)


def test_manager_uses_hybrid_crawler_for_auto_mode(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: DiscoveryBatchReport(),
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        crawl_engine="auto",
    )

    assert isinstance(manager._crawler, HybridCrawler)


def test_manager_uses_scrapling_crawler_when_selected(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: DiscoveryBatchReport(),
    )

    manager = CrawlerManager(
        config=_make_config(str(seed_file), str(sqlite_path)),
        crawl_engine="scrapling",
    )

    assert isinstance(manager._crawler, ScraplingCrawler)


def test_manager_can_ignore_blacklist(monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://news.example.com/story\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"
    blacklist_path = tmp_path / "domain_blacklist.txt"
    blacklist_path.write_text("example.com\n", encoding="utf-8")

    original_path = URLUtils._blacklist_path
    original_enabled = URLUtils._blacklist_enabled

    monkeypatch.setattr(
        "core.crawler_manager.discover_urls_from_queries_with_report",
        lambda *args, **kwargs: DiscoveryBatchReport(),
    )

    try:
        URLUtils.set_blacklist_path(str(blacklist_path))

        manager = CrawlerManager(
            config=_make_config(str(seed_file), str(sqlite_path)),
            ignore_blacklist=True,
        )
        manager.prepare_frontier()

        claim = manager.frontier.get_next_url()
        assert claim is not None
        assert claim.url == "https://news.example.com/story"
    finally:
        URLUtils.set_blacklist_path(str(original_path))
        URLUtils.set_blacklist_enabled(original_enabled)


# ---------------------------------------------------------------------------
# --runtime: wall-clock run-duration limit (core/crawler_manager.py's
# set_runtime_limit()/_runtime_watchdog()). See main_cli_test.py for the
# --max-pages/--indefinite-run/--runtime precedence tests -- these exercise
# the actual graceful-shutdown behavior at the manager/crawl-engine level.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_watchdog_sets_stop_event_and_runtime_expired_flag(monkeypatch, tmp_path):
    """Fast, deterministic unit test of the watchdog itself: no real sleep
    (asyncio.sleep is replaced with a no-op), no real crawl -- proves the
    watchdog's own contract (flip runtime_expired, set the crawl engine's
    _stop_event) independent of any particular crawl engine or timing."""
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://seed.example.com\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    manager = CrawlerManager(config=_make_config(str(seed_file), str(sqlite_path)))

    slept_for = []

    async def _fake_sleep(seconds):
        slept_for.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    manager.set_runtime_limit(30 * 60)
    assert manager.runtime_expired is False

    await manager._runtime_watchdog()

    assert slept_for == [30 * 60]
    assert manager.runtime_expired is True
    assert manager._crawler._stop_event.is_set()


async def _run_infinite_ping_pong_server():
    """Two pages that link to each other forever -- frontier exhaustion
    never happens on its own, so a run against this server only stops via
    an explicit limit (max-pages or --runtime), isolating the behavior
    under test."""
    app = web.Application()

    async def handler_root(request):
        return web.Response(
            text="<html><body><a href='/page'>page</a></body></html>",
            content_type="text/html",
        )

    async def handler_page(request):
        return web.Response(
            text="<html><body><a href='/'>root</a></body></html>",
            content_type="text/html",
        )

    app.router.add_get("/", handler_root)
    app.router.add_get("/page", handler_page)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


@pytest.mark.asyncio
async def test_runtime_limit_lets_crawl_run_past_a_tiny_default_max_pages(tmp_path):
    """End-to-end proof of the feature's whole point: with a --runtime
    limit active, the crawler must NOT stop at the (tiny, here) default
    max_pages ceiling -- it keeps going, exactly like --indefinite-run,
    until the runtime budget (a real, short sleep here -- not a fake clock,
    since this exercises the actual asyncio.sleep-driven watchdog
    end-to-end) elapses. Uses a real small delay (~1s) rather than
    mocking time -- negligible for the test suite, and the whole point is
    to prove the *actual* asyncio watchdog task races the *actual* crawl
    engine correctly, which a fully mocked clock can't demonstrate."""
    runner, base_url = await _run_infinite_ping_pong_server()
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text(f"{base_url}\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    config = _make_config(str(seed_file), str(sqlite_path))
    # The default rate_limit (1 req/s/domain) would only allow ~1 fetch
    # within this test's short runtime window regardless of the page cap --
    # unrelated to what this test is actually proving. Disabled here so
    # "kept crawling past max_pages=1" isn't confounded by rate limiting.
    config.crawler.rate_limit = 0.0

    try:
        manager = CrawlerManager(config=config)
        # Mirrors main.py's --runtime precedence: an active runtime limit
        # disables the default max_pages ceiling, same as --indefinite-run.
        manager.set_max_pages(None)
        manager.set_runtime_limit(1.0)

        await manager.run()

        assert manager.runtime_expired is True
        # Proves the crawl did not stop at max_pages=1 -- it kept running
        # (bounded only by the runtime budget) against the never-exhausting
        # ping-pong frontier.
        assert manager._crawler._pages_crawled > 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_without_runtime_limit_default_max_pages_still_stops_the_crawl(tmp_path):
    """Control case: omitting --runtime must leave existing --max-pages
    behavior completely unchanged (the crawl still stops at the default
    ceiling from _make_config, no runtime_expired flag)."""
    runner, base_url = await _run_infinite_ping_pong_server()
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text(f"{base_url}\n", encoding="utf-8")
    sqlite_path = tmp_path / "crawl.db"

    try:
        manager = CrawlerManager(
            config=_make_config(str(seed_file), str(sqlite_path)),  # max_pages=1
        )

        await manager.run()

        assert manager.runtime_expired is False
        assert manager._crawler._pages_crawled == 1
    finally:
        await runner.cleanup()
