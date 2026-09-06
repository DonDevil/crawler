"""Tests for bridge/result_consumer_main.py's --runtime CLI flag: argument
validation and deadline plumbing into
FingerprintResultConsumer.run_forever(). Never touches real Redis --
build_media_evidence_store()/FingerprintResultConsumer are replaced with
in-memory fakes, mirroring tests/bridge_main_runtime_test.py."""
from __future__ import annotations

import time

import pytest

import bridge.result_consumer_main as result_consumer_main


class _FakeStore:
    def close(self):
        pass


class _DummyConsumer:
    last_instance = None

    def __init__(self, store, consumer_name, priorities, consumer_group, block_ms, lease_ms, reclaim_batch_size):
        self.store = store
        self.metrics = "fake-metrics"
        self.deadline = "unset"
        self.stopped = False
        self.closed = False
        _DummyConsumer.last_instance = self

    def stop(self):
        self.stopped = True

    def run_forever(self, reclaim_interval_seconds=60.0, idle_sleep_seconds=2.0, deadline=None):
        self.deadline = deadline

    def process_one(self):
        return False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _patch_consumer_dependencies(monkeypatch):
    monkeypatch.setattr(result_consumer_main, "build_media_evidence_store", lambda config: _FakeStore())
    monkeypatch.setattr(result_consumer_main, "RedisMediaEvidenceStore", _FakeStore)
    monkeypatch.setattr(result_consumer_main, "FingerprintResultConsumer", _DummyConsumer)
    monkeypatch.setattr(result_consumer_main.signal, "signal", lambda *args, **kwargs: None)
    _DummyConsumer.last_instance = None


def test_runtime_default_disables_deadline(monkeypatch):
    monkeypatch.setattr("sys.argv", ["result_consumer_main"])

    result_consumer_main.main()

    assert _DummyConsumer.last_instance.deadline is None


def test_runtime_flag_computes_a_future_monotonic_deadline(monkeypatch):
    monkeypatch.setattr("sys.argv", ["result_consumer_main", "--runtime", "30"])

    before = time.monotonic()
    result_consumer_main.main()
    after = time.monotonic()

    deadline = _DummyConsumer.last_instance.deadline
    assert deadline is not None
    assert before + 30 * 60 <= deadline <= after + 30 * 60


def test_runtime_zero_is_valid(monkeypatch):
    monkeypatch.setattr("sys.argv", ["result_consumer_main", "--runtime", "0"])

    before = time.monotonic()
    result_consumer_main.main()

    deadline = _DummyConsumer.last_instance.deadline
    assert deadline is not None
    assert deadline <= before + 1.0


def test_runtime_rejects_values_below_negative_one(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["result_consumer_main", "--runtime", "-5"])

    with pytest.raises(SystemExit):
        result_consumer_main.main()

    assert "--runtime" in capsys.readouterr().err


def test_once_mode_ignores_runtime_and_never_calls_run_forever(monkeypatch):
    monkeypatch.setattr("sys.argv", ["result_consumer_main", "--once", "--runtime", "30"])

    result_consumer_main.main()

    assert _DummyConsumer.last_instance.deadline == "unset"
