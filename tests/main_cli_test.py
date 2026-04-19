"""Tests for main CLI behaviors."""

from __future__ import annotations

import main as app_main


class _DummyManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.max_pages = "unchanged"

    def clear_storage(self) -> None:
        pass

    def set_max_pages(self, max_pages):
        self.max_pages = max_pages

    async def run(self):
        return None


def test_indefinite_run_flag_disables_page_cap(monkeypatch):
    captured: dict[str, object] = {}

    def _make_manager(**kwargs):
        manager = _DummyManager(**kwargs)
        captured["manager"] = manager
        return manager

    monkeypatch.setattr(app_main, "CrawlerManager", _make_manager)

    def _fake_asyncio_run(coroutine):
        coroutine.close()
        return None

    monkeypatch.setattr(app_main.asyncio, "run", _fake_asyncio_run)
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--indefinite-run"],
    )

    app_main.main()

    manager = captured["manager"]
    assert manager.max_pages is None
