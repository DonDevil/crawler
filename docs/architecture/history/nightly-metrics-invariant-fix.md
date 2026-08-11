# Nightly benchmark metrics: invariant violation fix

Summary of an incident found in the 2026-08-10/11 overnight Redis benchmark
run (`benchmark/results/overnight_blast_redis.json`, ~5.16h, 25 async
workers, `rate_limit=0.3`): the reported counts were internally impossible --
`visited (14868) + failed_permanent (38503) = 53371`, which exceeds
`discovered_total (38550)` by 14,821. Every derived percentage in that file
(`failed_pct: 99.88%`, etc.) was therefore not trustworthy.

## Root cause

Two independent problems compounded to produce the impossible total. Neither
is a bug in the Redis frontier's claim/completion logic (`core/redis_frontier.py`);
no frontier, Lua-script, or crawler behavior changed as part of this fix.

**1. The report conflated "this run" with "this Redis namespace's entire
history".** `main.py` took a single counts snapshot *after* the run finished
and reported it as that run's numbers. But the Redis frontier is intentionally
never reset between runs (so `--unfinished` can resume queued/pending work),
so every counter (`urls:known`, `urls:visited`, `urls:failed_permanent`,
`urls:skipped`) is lifetime-cumulative for the `crawler` namespace on db 0 --
not scoped to any single process's execution.

**2. That lifetime keyspace is itself contaminated by older runs/schema
versions.** Direct inspection of the live Redis instance (still running
post-benchmark) found:
- ~80K stray legacy keys (`crawler:urls:metadata:http*`,
  `crawler:urls:metadata:https*`, `crawler:urls:queued`) matching a key
  pattern the current `RedisURLFrontier` never writes -- `tests/report_lib.py`
  already carried a docstring noting the *previous* report format read
  `urls:queued`/`urls:failed`, which "do not exist in the current keyspace";
  this is the leftover data from that era, never cleared.
- 7,365 URLs that are simultaneously members of `urls:visited` **and**
  `urls:failed_permanent` (verified via `SINTERCARD`/Lua on the live db).
- 7,457 `urls:visited` members that are not members of `urls:known` at all.

Both are provably impossible under the *current* single-run CAS design:
`_complete_claim_script` only ever finalizes a claim into exactly one
terminal SET, gated by an exactly-once token compare-and-swap, and a URL can
only reach a domain queue (and later a terminal state) after `add_url`'s
`SADD` to `urls:known`. The contamination therefore predates the current
schema/CAS design and accumulated because the namespace has never been reset
across however many historical benchmark sessions used it.

## Files changed

- `main.py` -- now takes a frontier snapshot immediately *before*
  `manager.run()` starts (previously only captured after), so the report can
  isolate this run's activity from the namespace's lifetime state.
- `tests/report_lib.py`
  - `terminal_states_are_consistent(snapshot)`: returns whether
    `visited + failed_permanent + skipped <= discovered_total` holds for a
    snapshot (`None` if any operand is missing).
  - `counts_with_percentages()` / `compute_throughput()`: now null out
    `visited_pct` / `failed_pct` / `skipped_pct` / `completed_per_sec`
    instead of computing a number when the invariant above doesn't hold, and
    attach an `invariant_violation` explanation string.
  - `build_this_run(pre_run_snapshot, post_run_snapshot, ...)` (new): reports
    `discovered_unique` / `visited_unique` / `failed_permanent_unique` /
    `skipped_unique` as `(post - pre)` deltas, plus `queued_current` /
    `inflight_current` / `retry_scheduled_current` from the post-run
    snapshot. Deltas isolate this run's activity even when the lifetime
    totals are contaminated, because every counter here only ever grows
    (SADD-only, no SREM) and pre-existing contamination is present in both
    the pre- and post-run snapshots equally, so it cancels out in the
    subtraction. `attempted_unique` / `completion_pct` / `success_rate_pct`
    are only populated when this run's own deltas satisfy the same
    mutual-exclusivity invariant; otherwise `null` with an explanation.
  - `build_report()` now accepts an optional `pre_run_snapshot` and includes
    the new `this_run` section (`None` if no pre-run snapshot was captured,
    e.g. older report tooling or `tests/report.py`'s ad-hoc point-in-time
    mode).
  - `render_human_report()`: added a "THIS RUN (run-scoped counts)" section
    and invariant warnings in the lifetime "URL STATE" section.
- `tests/report_test.py` -- added regression tests:
  - `test_invariant_violation_nulls_lifetime_percentages_not_fabricated`
    (reproduces the exact real numbers from the overnight run).
  - `test_invariant_holds_reports_real_percentages` (clean snapshot still
    reports real percentages, not always-null).
  - `test_this_run_deltas_isolate_run_from_lifetime_contamination` (a
    pre-run snapshot with pre-existing contamination + a clean this-run
    delta still yields valid `completion_pct`).
  - `test_this_run_none_without_pre_run_snapshot`.

## What was verified

- `pytest tests/report_test.py tests/redis_frontier_test.py
  tests/frontier_redis_failure_semantics_test.py` -- 52 passed.
- `python tests/report.py --redis` run read-only against the live,
  contaminated `crawler` namespace: `visited_pct`/`failed_pct` now report
  `N/A` with an explicit warning instead of the previous misleading 99.88%.

## Remaining limitations

- The `this_run` section only exists for runs launched via
  `python main.py ... --output <file>` going forward -- it needs a pre-run
  snapshot that was never captured for the existing
  `overnight_blast_redis.json`, so that file was left unmodified (as
  instructed, no historical data was rewritten or reconstructed).
- The underlying Redis namespace (`crawler`, db 0) still contains the legacy
  contamination described above. It was not cleared or migrated -- that
  would be a frontier/operational change, out of this fix's scope. Nightly
  comparisons should read the new `this_run` section, not the lifetime
  `counts` section, until the namespace is reset (e.g. `RedisURLFrontier.clear()`
  or a fresh `redis_namespace`/`redis_db` per benchmark series).
- `docs/architecture/history/test-analysis.md`'s historical April/July
  figures were computed under whatever report logic existed at the time and
  are not recalculated or endorsed by this fix; they are retained purely as
  historical record.
