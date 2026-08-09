# Benchmark blacklist-contamination fix

See `docs/architecture/benchmark_bug_audit.md` for the full root-cause
investigation. Summary of the incident: the production
`datasets/domain_blacklist.txt` was contaminated with the synthetic
`bench0.example.test` .. `bench19.example.test` domains that
`tests/benchmarks/common.py` generates, so `URLUtils.is_blacklisted()`
rejected every synthetic benchmark URL before it reached either frontier
(`inserted = 0`, `claims = 0`, `completions = 0` on every run). No frontier,
Lua script, or crawler code was at fault — the frontier implementations
correctly rejected URLs on blacklisted domains, which is their job.

This document covers the fix: benchmark-harness isolation only. No
production code changed.

## Files changed

- `tests/benchmarks/common.py`
  - Added `isolate_blacklist(directory=None) -> Path`: points `URLUtils` at
    a fresh, empty temp file via the existing `URLUtils.set_blacklist_path()`
    mechanism, and registers an `atexit` cleanup for it.
  - `make_domains(domain_count, prefix="bench", run_id="")`: now embeds
    `run_id` in every generated domain name when given (e.g.
    `bench-fb1691584922-0.example.test` instead of `bench0.example.test`),
    matching the per-run-unique approach `priority_ratelimit.py` already
    used for its own domains.
  - `make_synthetic_urls(...)` now forwards its existing `run_id` parameter
    into `make_domains()` (previously `run_id` only disambiguated the URL
    path, not the domain — the actual gap that let a stale blacklist entry
    or leftover frontier state permanently poison future runs).
- `tests/benchmarks/frontier_benchmark.py`
  - `main()` calls `common.isolate_blacklist()` before building the
    frontier or any synthetic workload.
- `tests/benchmarks/distributed_benchmark.py`
  - `main()` (coordinator process) calls `common.isolate_blacklist()` and
    passes the resulting path to every worker process explicitly.
  - `_worker_main()` (each independent OS worker process) takes a new
    `blacklist_path` argument and calls `URLUtils.set_blacklist_path()`
    with it first thing, before building its own frontier connection. This
    is necessary because `get_next_url()` re-checks the blacklist on every
    claim, not just on insert — and passing the path explicitly (rather
    than relying on `multiprocessing`'s fork start method to inherit the
    class attribute) keeps this correct regardless of platform/start
    method.
- `tests/benchmark_harness_test.py` (new)
  - Regression tests for the isolation behavior (see below).
- `docs/architecture/benchmark_bug_fix.md` (this file)

Nothing else changed. Frontier implementations, Redis Lua scripts, Redis
data structures, claim/recovery logic, heartbeat, rate limiting, the
scheduler, worker code, and `URLUtils`'s production blacklist behavior are
all untouched. `crash_recovery.py`, `heartbeat_endurance.py`, and
`priority_ratelimit.py` were not modified — their domain-naming schemes
were already per-run-unique / didn't go through `common.make_domains()`
(confirmed unaffected by the audit).

## Exact behavior changed

Before: `frontier_benchmark.py` and `distributed_benchmark.py` called
`URLUtils`'s default blacklist path, i.e. the real
`datasets/domain_blacklist.txt` — both reading it (via
`is_blacklisted()`/`clean_url()` inside every `add_url()` and
`get_next_url()` call) and, via `should_auto_blacklist()` /
`add_to_blacklist()`, capable of writing to it. Synthetic domains were
static (`bench0.example.test` .. `bench{N-1}.example.test`), so a domain
blacklisted once (accidentally or otherwise) stayed poisoned for every
future run, forever.

After:
- Every benchmark run points `URLUtils` at a fresh temp file
  (`isolate_blacklist()`), created empty, and never touches
  `datasets/domain_blacklist.txt` — not for reads, not for
  auto-blacklist writes.
- Synthetic domains embed the run's `run_id`, so two runs (or a run and
  any previous poisoned state) can never share a domain name.
- Blacklist *checking* itself is unchanged — `is_blacklisted()`/
  `clean_url()` still run exactly as in production, just against the
  isolated file, so the harness still exercises the real blacklist code
  path (not a bypass/mock).

## Tests run

```
python3 -m pytest tests/benchmark_harness_test.py -v
```

```
tests/benchmark_harness_test.py::test_isolate_blacklist_points_away_from_production PASSED
tests/benchmark_harness_test.py::test_production_blacklist_never_consulted_for_synthetic_urls PASSED
tests/benchmark_harness_test.py::test_synthetic_domains_are_unique_between_runs PASSED
tests/benchmark_harness_test.py::test_fresh_benchmark_can_insert_synthetic_urls_under_isolation PASSED
tests/benchmark_harness_test.py::test_blacklist_checks_still_function_inside_isolated_environment PASSED
5 passed
```

Also re-ran the existing, unrelated suites that touch `URLUtils`/frontier
code to confirm no regression:

```
python3 -m pytest tests/url_utils_test.py tests/frontier_test.py tests/redis_frontier_test.py -q
38 passed
```

## Diagnostic results (tiny runs, not the full 20K benchmark)

All three harness entry points, 8-10 synthetic URLs, `--no-rate-limit`
(local/single-process throughput doesn't need rate gating to prove the
fix), isolated Redis db 2 under one-off `bench*_diag` namespaces (cleared
afterward):

| Run | inserted | claims | completions |
|---|---|---|---|
| `frontier_benchmark.py --frontier local` (8 urls) | 8 | 8 | 8 |
| `frontier_benchmark.py --frontier redis` (10 urls) | 10 | 10 | 10 |
| `distributed_benchmark.py` (10 urls, 2 worker processes) | 10 | 10 | 10 |

All three: `inserted > 0`, `claims > 0`, `completions > 0`,
`duplicate_successful_claims: 0`. The distributed run in particular
confirms the worker-process blacklist-path forwarding works (workers claim
via a freshly-forked/started OS process, not the coordinator's memory).

Per instructions, the full 20K benchmark was **not** re-run yet — that's a
separate follow-up once this fix is reviewed.

## Was the production blacklist touched?

**No.** `datasets/domain_blacklist.txt` was not modified by this fix (it's
gitignored local runtime state, and no code path here writes to it — all
writes now go to per-run temp files). Verified line count (1463) and
content unchanged before/after.

## Contamination still present — cleanup required separately

The 20 synthetic entries diagnosed in `benchmark_bug_audit.md` are still
in `datasets/domain_blacklist.txt`, lines 1444-1463:

```
bench0.example.test
bench1.example.test
...
bench19.example.test
```

This fix does not remove them (out of scope — "do not blindly rewrite the
production blacklist"). Since the harness no longer reads this file at
all, their presence no longer affects any benchmark run — but they're
still noise in a production file if you want it clean. Safe, scoped
cleanup command (deletes only exact `benchN.example.test` lines, nothing
else):

```bash
sed -i '/^bench[0-9]\+\.example\.test$/d' datasets/domain_blacklist.txt
```

Run this by hand if/when desired; it was intentionally not run as part of
this change.
