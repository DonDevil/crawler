"""Tests for main CLI behaviors."""

from __future__ import annotations

import pytest

import main as app_main


class _DummyManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.max_pages = "unchanged"
        self.runtime_seconds = "unchanged"
        self.runtime_expired = False

    def clear_storage(self) -> None:
        pass

    def set_max_pages(self, max_pages):
        self.max_pages = max_pages

    def set_runtime_limit(self, runtime_seconds):
        self.runtime_seconds = runtime_seconds

    async def run(self):
        return None


def _run_main_with_argv(monkeypatch, argv: list[str]) -> _DummyManager:
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
    monkeypatch.setattr("sys.argv", argv)

    app_main.main()

    return captured["manager"]


def test_indefinite_run_flag_disables_page_cap(monkeypatch):
    manager = _run_main_with_argv(monkeypatch, ["main.py", "--indefinite-run"])

    assert manager.max_pages is None


def test_indefinite_run_overrides_explicit_max_pages(monkeypatch):
    """Existing precedent (unchanged by this feature): --indefinite-run
    unconditionally disables the page cap even if --max-pages was also
    given explicitly."""
    manager = _run_main_with_argv(monkeypatch, ["main.py", "--indefinite-run", "--max-pages", "50"])

    assert manager.max_pages is None


def test_runtime_omitted_leaves_max_pages_and_runtime_untouched(monkeypatch):
    """No --runtime -> behavior must be identical to before this feature
    existed: set_max_pages() is never called (config default stands) and
    set_runtime_limit() is called with None (disabled)."""
    manager = _run_main_with_argv(monkeypatch, ["main.py"])

    assert manager.max_pages == "unchanged"
    assert manager.runtime_seconds is None


def test_runtime_flag_disables_default_max_pages_cap(monkeypatch):
    """--runtime alone must behave like --indefinite-run for page
    semantics: the default --max-pages ceiling is disabled so the crawler
    doesn't stop early after the default page count."""
    manager = _run_main_with_argv(monkeypatch, ["main.py", "--runtime", "30"])

    assert manager.max_pages is None
    assert manager.runtime_seconds == 30 * 60


def test_indefinite_run_with_runtime_is_same_as_runtime_alone(monkeypatch):
    manager = _run_main_with_argv(monkeypatch, ["main.py", "--indefinite-run", "--runtime", "30"])

    assert manager.max_pages is None
    assert manager.runtime_seconds == 30 * 60


def test_runtime_with_explicit_max_pages_preserves_explicit_bound(monkeypatch):
    """Unlike --indefinite-run, --runtime must NOT silently override an
    explicit --max-pages -- both bounds apply; whichever is hit first
    stops the run."""
    manager = _run_main_with_argv(monkeypatch, ["main.py", "--max-pages", "50", "--runtime", "30"])

    assert manager.max_pages == 50
    assert manager.runtime_seconds == 30 * 60


def test_runtime_zero_is_valid_and_disables_default_max_pages(monkeypatch):
    manager = _run_main_with_argv(monkeypatch, ["main.py", "--runtime", "0"])

    assert manager.max_pages is None
    assert manager.runtime_seconds == 0.0


def test_runtime_rejects_values_below_negative_one(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["main.py", "--runtime", "-5"])

    with pytest.raises(SystemExit):
        app_main.main()

    assert "--runtime" in capsys.readouterr().err
