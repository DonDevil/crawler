"""Tests for crawler manager startup modes."""

from discovery.search_engine_discovery import DiscoveryBatchReport, QueryDiscoveryReport

from core.config import Config, CrawlerConfig, SearchConfig, StorageConfig
from core.crawler_manager import CrawlerManager
from crawler.http_crawler import HTTPCrawler


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

    assert manager.frontier.get_next_url() == "https://query.example.com/"
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

    resumed = {manager.frontier.get_next_url(), manager.frontier.get_next_url()}
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

    assert manager.frontier.get_next_url() == "http://resumehiddenresumehidden.onion/"


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
