"""Regression tests for the benchmark-harness blacklist isolation fix.

See docs/architecture/benchmark_bug_audit.md and
docs/architecture/benchmark_bug_fix.md: the production
`datasets/domain_blacklist.txt` got contaminated with synthetic
`bench0..19.example.test` entries, silently zeroing out every insert in
`frontier_benchmark.py`/`distributed_benchmark.py`. These tests prove the
harness can no longer read from or write to the production blacklist, and
that synthetic domains can't collide across runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BENCHMARKS_DIR = Path(__file__).resolve().parent / "benchmarks"
if str(_BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_DIR))

import common  # noqa: E402
from core.url_frontier import URLFrontier  # noqa: E402
from utils.url_utils import URLUtils  # noqa: E402

_PRODUCTION_BLACKLIST_PATH = Path("datasets/domain_blacklist.txt").resolve()


def _save_urlutils_state():
    return URLUtils._blacklist_path, URLUtils._blacklist_enabled


def _restore_urlutils_state(state) -> None:
    path, enabled = state
    URLUtils.set_blacklist_path(str(path))
    URLUtils._blacklist_enabled = enabled


def test_isolate_blacklist_points_away_from_production():
    state = _save_urlutils_state()
    try:
        isolated_path = common.isolate_blacklist()

        assert isolated_path.resolve() != _PRODUCTION_BLACKLIST_PATH
        assert URLUtils._blacklist_path.resolve() == isolated_path.resolve()
        assert isolated_path.read_text(encoding="utf-8") == ""
    finally:
        _restore_urlutils_state(state)


def test_production_blacklist_never_consulted_for_synthetic_urls(tmp_path):
    """Reproduces the exact incident: entries that really are in the
    production blacklist (the contaminated bench0-19 domains) must have no
    effect once the harness has isolated the blacklist path -- and the
    isolated file must never inherit production content."""

    state = _save_urlutils_state()
    try:
        # Confirm this repo's production file is (still) contaminated,
        # matching the incident this fix addresses -- if not, this half of
        # the test is a no-op, but the assertions below still hold.
        production_text = _PRODUCTION_BLACKLIST_PATH.read_text(encoding="utf-8")
        contaminated = "bench0.example.test" in production_text

        if contaminated:
            URLUtils.set_blacklist_path(str(_PRODUCTION_BLACKLIST_PATH))
            assert URLUtils.is_blacklisted("https://bench0.example.test/x") is True

        isolated_path = common.isolate_blacklist(directory=str(tmp_path))
        assert isolated_path.read_text(encoding="utf-8") == ""
        assert URLUtils.is_blacklisted("https://bench0.example.test/x") is False
    finally:
        _restore_urlutils_state(state)


def test_synthetic_domains_are_unique_between_runs():
    domains_a = common.make_domains(5, run_id="runA")
    domains_b = common.make_domains(5, run_id="runB")

    assert set(domains_a).isdisjoint(domains_b)
    assert len(set(domains_a)) == 5
    assert len(set(domains_b)) == 5

    urls_a = common.make_synthetic_urls(5, 5, lambda rng: 10, None, run_id="runA")
    urls_b = common.make_synthetic_urls(5, 5, lambda rng: 10, None, run_id="runB")
    domains_from_urls_a = {URLUtils.extract_domain(url) for url, _ in urls_a}
    domains_from_urls_b = {URLUtils.extract_domain(url) for url, _ in urls_b}
    assert domains_from_urls_a.isdisjoint(domains_from_urls_b)


def test_fresh_benchmark_can_insert_synthetic_urls_under_isolation(tmp_path):
    state = _save_urlutils_state()
    try:
        common.isolate_blacklist(directory=str(tmp_path))

        urls = common.make_synthetic_urls(
            10, 3, lambda rng: 10, None, run_id="freshrun"
        )

        frontier = URLFrontier(rate_limit=0.0)
        try:
            inserted = sum(1 for url, priority in urls if frontier.add_url(url, priority=priority))
            assert inserted == 10

            claims = []
            for _ in range(10):
                claim = frontier.get_next_url()
                if claim is None:
                    break
                claims.append(claim)
                frontier.mark_visited(claim)

            assert len(claims) == 10
            status = frontier.get_status_counts()
            assert status.get("visited", 0) == 10
        finally:
            frontier.close()
    finally:
        _restore_urlutils_state(state)


def test_blacklist_checks_still_function_inside_isolated_environment(tmp_path):
    state = _save_urlutils_state()
    try:
        isolated_path = common.isolate_blacklist(directory=str(tmp_path))
        isolated_path.write_text("blocked-example.test\n", encoding="utf-8")

        assert URLUtils.is_blacklisted("https://blocked-example.test/x") is True
        assert URLUtils.clean_url("https://blocked-example.test/x") is None
        assert URLUtils.clean_url("https://not-blocked-example.test/x") is not None
    finally:
        _restore_urlutils_state(state)
