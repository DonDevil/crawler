"""Tests for bridge/main.py's --runtime CLI flag: argument validation and
deadline plumbing into CrawlerFingerprinterBridge.run_forever(). Never
touches real Redis -- build_media_evidence_store()/CrawlerFingerprinterBridge
are replaced with in-memory fakes, mirroring tests/main_cli_test.py's
_DummyManager approach for the crawler's own --runtime tests."""
from __future__ import annotations

import time

import pytest

import bridge.main as bridge_main


class _FakeStore:
    def close(self):
        pass


class _DummyBridge:
    last_instance = None

    def __init__(self, store, worker_id, config):
        self.store = store
        self.worker_id = worker_id
        self.config = config
        self.metrics = "fake-metrics"
        self.deadline = "unset"
        self.stopped = False
        _DummyBridge.last_instance = self

    def stop(self):
        self.stopped = True

    def run_forever(self, deadline=None):
        self.deadline = deadline

    def process_one(self):
        return False


@pytest.fixture(autouse=True)
def _patch_bridge_dependencies(monkeypatch):
    monkeypatch.setattr(bridge_main, "build_media_evidence_store", lambda config: _FakeStore())
    monkeypatch.setattr(bridge_main, "RedisMediaEvidenceStore", _FakeStore)
    monkeypatch.setattr(bridge_main, "CrawlerFingerprinterBridge", _DummyBridge)
    monkeypatch.setattr(bridge_main.signal, "signal", lambda *args, **kwargs: None)
    _DummyBridge.last_instance = None


def test_runtime_default_disables_deadline(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bridge.main"])

    bridge_main.main()

    assert _DummyBridge.last_instance.deadline is None


def test_runtime_flag_computes_a_future_monotonic_deadline(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bridge.main", "--runtime", "30"])

    before = time.monotonic()
    bridge_main.main()
    after = time.monotonic()

    deadline = _DummyBridge.last_instance.deadline
    assert deadline is not None
    assert before + 30 * 60 <= deadline <= after + 30 * 60


def test_runtime_zero_is_valid(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bridge.main", "--runtime", "0"])

    before = time.monotonic()
    bridge_main.main()

    deadline = _DummyBridge.last_instance.deadline
    assert deadline is not None
    assert deadline <= before + 1.0  # ~now, not 30 minutes out


def test_runtime_rejects_values_below_negative_one(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["bridge.main", "--runtime", "-5"])

    with pytest.raises(SystemExit):
        bridge_main.main()

    assert "--runtime" in capsys.readouterr().err


def test_once_mode_ignores_runtime_and_never_calls_run_forever(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bridge.main", "--once", "--runtime", "30"])

    bridge_main.main()

    assert _DummyBridge.last_instance.deadline == "unset"  # run_forever() was never called
