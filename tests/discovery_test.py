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
