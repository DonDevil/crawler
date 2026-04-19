"""Tests for URL database behavior."""

from storage.url_database import URLDatabase


def test_url_database_can_be_cleared(tmp_path):
    db_path = tmp_path / "crawl.db"
    database = URLDatabase(path=str(db_path))

    try:
        database.add_url("https://example.com/1", status="queued")
        database.add_url("https://example.com/2", status="visited")

        database.clear()

        assert list(database.get_all_urls()) == []
        assert database.get_status_counts() == {}
    finally:
        database.close()
