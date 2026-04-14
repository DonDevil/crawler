"""Discovery module unit tests."""

from discovery import search_engine_discovery
from discovery.piracy_site_seeds import load_seeds


class _SuccessfulEngine:
    def __init__(self, name, urls):
        self.name = name
        self._urls = urls

    def search(self, query: str, max_results: int = 20):
        return self._urls[:max_results]


class _FailingEngine:
    name = "failing"

    def search(self, query: str, max_results: int = 20):
        raise RuntimeError("boom")


def test_load_seeds_filters_comments_and_empty_lines(tmp_path):
    file_path = tmp_path / "seeds.txt"
    file_path.write_text("""# comment\nhttps://example.com\n\n# another\nhttp://test.local\n""")

    seeds = list(load_seeds(str(file_path)))
    assert seeds == ["https://example.com", "http://test.local"]


def test_discover_urls_reports_engine_errors_and_deduplicates(monkeypatch):
    monkeypatch.setattr(
        search_engine_discovery,
        "build_search_engines",
        lambda *args, **kwargs: [
            _SuccessfulEngine("engine-a", [
                "https://example.com/page?utm_source=test",
                "https://example.com/other",
            ]),
            _SuccessfulEngine("engine-b", [
                "https://example.com/page",
                "https://second.example.com/path",
            ]),
            _FailingEngine(),
        ],
    )

    report = search_engine_discovery.discover_urls_from_queries_with_report(["piracy query"])

    assert report.urls == [
        "https://example.com/page",
        "https://example.com/other",
        "https://second.example.com/path",
    ]
    assert report.query_reports[0].engine_results == {"engine-a": 2, "engine-b": 2}
    assert "failing" in report.query_reports[0].engine_errors


def test_discover_urls_scores_onion_and_higher_priority_engines(monkeypatch):
    monkeypatch.setattr(
        search_engine_discovery,
        "build_search_engines",
        lambda *args, **kwargs: [
            _SuccessfulEngine("bing", ["https://surface.example.com/title"]),
            _SuccessfulEngine("torch", ["http://hiddenexamplehiddenexample.onion/"]),
        ],
    )

    report = search_engine_discovery.discover_urls_from_queries_with_report(
        ["query"],
        engine_priorities={"bing": 5, "torch": 0},
        onion_priority_boost=2,
    )

    assert report.discovered_items[0].url == "http://hiddenexamplehiddenexample.onion/"
    assert report.discovered_items[0].priority < report.discovered_items[1].priority


def test_blocked_engine_is_backed_off_for_later_queries(monkeypatch):
    class _BlockedEngine:
        name = "yandex"

        def search(self, query: str, max_results: int = 20):
            raise search_engine_discovery.SearchEngineBlockedError("captcha blocked")

    class _TrackingEngine:
        name = "bing"

        def __init__(self):
            self.calls = []

        def search(self, query: str, max_results: int = 20):
            self.calls.append(query)
            return [f"https://example.com/{query}"]

    tracking_engine = _TrackingEngine()
    monkeypatch.setattr(
        search_engine_discovery,
        "build_search_engines",
        lambda *args, **kwargs: [_BlockedEngine(), tracking_engine],
    )

    report = search_engine_discovery.discover_urls_from_queries_with_report(
        ["first", "second"],
        blocked_engine_cooldown_queries=10,
    )

    assert report.query_reports[0].engine_errors["yandex"] == "captcha blocked"
    assert "yandex" in report.query_reports[1].skipped_engines
    assert tracking_engine.calls == ["first", "second"]


def test_get_engine_names_for_scope_filters_enabled_engines():
    enabled = ["duckduckgo", "bing", "ahmia", "torch"]

    assert search_engine_discovery.get_engine_names_for_scope("surface-web", enabled) == [
        "duckduckgo",
        "bing",
    ]
    assert search_engine_discovery.get_engine_names_for_scope("dark-web", enabled) == [
        "ahmia",
        "torch",
    ]
    assert search_engine_discovery.get_engine_names_for_scope(None, enabled) == enabled
