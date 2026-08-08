"""Tests for crawler manager startup modes."""

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
