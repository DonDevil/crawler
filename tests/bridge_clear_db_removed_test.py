"""Regression tests locking in the removal of `bridge.main --clear-db` /
`bridge.result_consumer_main --clear-db` (docs/architecture/history/
clear-db-ownership-audit.md).

Neither the bridge nor the result consumer owns any Redis namespace of its
own -- every key either process touches belongs to the crawler's own
`evidence:*` schema (`python main.py --clear-db`) or the fingerprinter's own
`fingerprint:*` schema (`python -m target.cli clear-db`, sibling repo).
Deleting the fingerprinter's `fingerprint:jobs:stream:{priority}` key from
an unrelated process (as the removed `bridge.redis_reset.clear_fingerprint_namespace`
did) crashed a live fingerprint worker -- see that doc for the full root
cause. These tests just prove the flag is actually gone from both
entrypoints' CLIs, not merely undocumented.

Invoked as real subprocesses against the actual CLI entrypoints (not by
reaching into argparse internals) so this fails loudly if `--clear-db` is
ever silently reintroduced. No Redis or config file needed -- argparse
rejects the unrecognized flag before either entrypoint touches config or
Redis.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_with_clear_db(module: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, "--clear-db"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_REPO_ROOT),
    )


def test_bridge_main_rejects_clear_db_flag():
    proc = _run_with_clear_db("bridge.main")
    assert proc.returncode == 2, proc.stderr
    assert "unrecognized arguments" in proc.stderr
    assert "--clear-db" in proc.stderr


def test_bridge_result_consumer_main_rejects_clear_db_flag():
    proc = _run_with_clear_db("bridge.result_consumer_main")
    assert proc.returncode == 2, proc.stderr
    assert "unrecognized arguments" in proc.stderr
    assert "--clear-db" in proc.stderr


def test_bridge_main_help_does_not_mention_clear_db():
    proc = subprocess.run(
        [sys.executable, "-m", "bridge.main", "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "--clear-db" not in proc.stdout


def test_bridge_result_consumer_main_help_does_not_mention_clear_db():
    proc = subprocess.run(
        [sys.executable, "-m", "bridge.result_consumer_main", "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "--clear-db" not in proc.stdout


def test_bridge_redis_reset_module_no_longer_exists():
    assert not (_REPO_ROOT / "bridge" / "redis_reset.py").exists()
