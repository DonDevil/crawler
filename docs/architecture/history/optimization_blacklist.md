# Optimization Phase 1 — Blacklist Cache Fix

Status: **implemented and tested.** This is the first targeted implementation
following `docs/architecture/frontier-optimization-audit.md` (§4.4, §9 item 1,
§12 item 1). Scope was intentionally limited to that single finding.

## Problem

`URLUtils._ensure_blacklist_file_exists()` called `Path.touch(exist_ok=True)`
unconditionally on every blacklist check. `touch()` always updates a file's
mtime, even when the file already exists — which immediately invalidated the
mtime-based cache check in `_reload_blacklist_if_needed()` (`cls._blacklist_mtime_ns
== stat.st_mtime_ns`) before it ever got a chance to short-circuit. The result:
every call to `is_blacklisted()` / `add_url()` / `get_next_url()` /
`get_link_priority()` forced a full re-read and re-parse of
`datasets/domain_blacklist.txt` (1,463 lines), the dominant cost identified in
the audit (measured 0.08ms raw Redis round trip vs. 6.07ms full call — a 77x
gap).

## Fix

**File:** `utils/url_utils.py`, `URLUtils._ensure_blacklist_file_exists()`.

Before:

```python
@classmethod
def _ensure_blacklist_file_exists(cls) -> None:
    cls._blacklist_path.parent.mkdir(parents=True, exist_ok=True)
    cls._blacklist_path.touch(exist_ok=True)
```

After:

```python
@classmethod
def _ensure_blacklist_file_exists(cls) -> None:
    if cls._blacklist_path.exists():
        return
    cls._blacklist_path.parent.mkdir(parents=True, exist_ok=True)
    cls._blacklist_path.touch(exist_ok=True)
```

The directory/file creation path now only runs when the blacklist file is
actually missing. An already-existing file's mtime is left untouched, so the
mtime comparison in `_reload_blacklist_if_needed()` works as originally
designed: the cache is only invalidated when the file's content actually
changes (e.g. via `add_to_blacklist()` / `ensure_blacklist_seeded()`, which
already explicitly reset `_blacklist_mtime_ns = None` after writing — that
invalidation path was correct and untouched).

Nothing else changed:
- Blacklist file format, path, and default-seeding behavior are unchanged.
- Automatic creation of a missing blacklist file (including parent
  directories) is preserved.
- No Redis frontier code, Lua scripts, Frontier APIs, asyncio/thread-pool
  behavior, crawler worker logic, domain scheduling, or retry/recovery/
  heartbeat behavior was touched.
- No other audit finding (§4.1–§4.3, §4.5–§4.9, §6, §8) was addressed. In
  particular §4.5 (blocking blacklist calls on the event loop thread) is
  explicitly deferred per the audit's own recommendation (§9 item 2, §12 item
  6) to re-measure after this fix before deciding whether it's still needed.

## Tests added

**File:** `tests/url_utils_test.py` — 4 new focused tests, alongside the 10
pre-existing blacklist/URL tests (all of which still pass unmodified):

1. `test_missing_blacklist_file_is_created` — a missing blacklist file
   (including a missing parent directory) is created on first use.
2. `test_checking_blacklist_does_not_modify_existing_file` — an existing
   file's mtime and content are unchanged across 20+ `is_blacklisted()` calls.
3. `test_repeated_is_blacklisted_calls_do_not_force_file_reload` —
   monkeypatches `open` to count reads of the blacklist file; once the
   in-memory cache is warm, repeated `is_blacklisted()` calls trigger zero
   further file reads.
4. `test_actual_blacklist_file_modification_is_detected_and_reloaded` — a
   real external edit to the blacklist file (a newly added domain) is picked
   up on the very next `is_blacklisted()` call, proving the cache still
   invalidates correctly on genuine content changes.

Tests 2 and 3 were verified to **fail against the pre-fix code** (via
`git stash` on `utils/url_utils.py` only) and **pass against the fix**,
confirming they actually exercise the regression rather than passing
vacuously.

## Test results

```
./env/bin/python -m pytest tests/url_utils_test.py -v
```

All 14 tests passed (10 pre-existing + 4 new):

- `test_clean_url_rejects_single_label_hosts` — PASSED
- `test_clean_url_rejects_markup_artifacts` — PASSED
- `test_is_onion_url_detects_hidden_services` — PASSED
- `test_clean_url_uses_live_domain_blacklist_reload` — PASSED
- `test_blacklist_is_seeded_with_default_non_target_domains` — PASSED
- `test_irrelevant_domains_are_auto_persisted_to_blacklist` — PASSED
- `test_suspicious_cross_domain_ad_redirect_is_detected` — PASSED
- `test_same_site_links_get_higher_priority_than_external_links` — PASSED
- `test_adult_content_domains_are_auto_filtered_and_blacklisted` — PASSED
- `test_should_queue_link_rejects_adult_cross_domain_targets` — PASSED
- `test_missing_blacklist_file_is_created` — PASSED
- `test_checking_blacklist_does_not_modify_existing_file` — PASSED
- `test_repeated_is_blacklisted_calls_do_not_force_file_reload` — PASSED
- `test_actual_blacklist_file_modification_is_detected_and_reloaded` — PASSED

No large benchmark campaign was run, per instruction. No other audit
recommendation was implemented in this phase.

## Next steps (not done in this phase)

Per the audit's recommended order (§12): re-run a small subset of the Step 6
throughput benchmarks (`frontier_benchmark.py --frontier redis --workers 1`
and `--workers 4`, `--no-rate-limit`) to establish the real post-fix ceiling,
then re-evaluate whether §4.5 (thread-offloading the blacklist check) is still
needed before touching anything else.
