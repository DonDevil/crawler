# Frontier validation & benchmark harness (Step 6)

Small, deterministic, manually-run scripts for comparing the local/SQLite
frontier against the Redis frontier and validating Redis's distributed
behavior (crash recovery, heartbeat renewal, priority/rate-limit
scheduling). See `docs/architecture/frontier-adr.md` and
`docs/architecture/frontier-step1.md`..`frontier-step5.md` for the design
these scripts exercise. **No frontier code, Lua scripts, or crawler worker
logic is touched by anything here** — these scripts only call the existing
public `Frontier` API (`core/frontier.py`) against synthetic workloads; they
do not perform real HTTP fetches or a real crawl.

None of these files match pytest's `test_*.py`/`*_test.py` discovery
pattern on purpose, so `pytest` never auto-collects them — they're CLI
tools you run by hand, not part of the automated test suite.

All Redis-backed scripts default to **db 2**, under namespaces prefixed
`bench*` — distinct from production (`db 0`, namespace `crawler`) and the
existing pytest Redis suite (`db 1`, namespace `test_crawler`). Every
script clears its own namespace before running (unless `--no-clear` is
passed where available), so runs are repeatable.

## Scripts

| Script | Purpose |
|---|---|
| `frontier_benchmark.py` | Single-process throughput/latency benchmark, local or Redis |
| `distributed_benchmark.py` | N independent OS processes racing against one shared Redis frontier |
| `crash_recovery.py` | Deterministic kill-mid-claim → lease expiry → reclaim → re-claim timeline |
| `heartbeat_endurance.py` | Slow synthetic fetch exceeding `lease_ttl`, heartbeat on vs. off |
| `priority_ratelimit.py` | Reports actual claim order across domains/priorities/rate gates |

Every script accepts `--output <path>` (writes JSON, or CSV with
`--format csv`) in addition to printing the result to stdout. Every
Redis-backed script accepts `--redis-host/--redis-port/--redis-db/
--namespace/--rate-limit/--max-retries/--base-backoff/--max-backoff/
--lease-ttl`; run `--help` on any script for the full flag list.

## 1. Throughput benchmark

Single process; workers are real OS threads calling the frontier's
synchronous API directly (the same execution boundary `AsyncFrontier` uses
for Redis via `asyncio.to_thread`). Measures insert rate, claim rate,
completion rate, claim/completion latency percentiles, and flags any
**duplicate successful claim** (the same URL completed as `visited` more
than once — a claim-safety violation, should always be 0).

```bash
python tests/benchmarks/frontier_benchmark.py --frontier local --urls 10000 --workers 4
python tests/benchmarks/frontier_benchmark.py --frontier redis --urls 10000 --workers 4

# Heavier, more realistic mix: many domains, skewed priorities, some retries
python tests/benchmarks/frontier_benchmark.py --frontier redis --urls 20000 --workers 8 \
    --domains 40 --priority-distribution weighted:1:0.1,5:0.3,10:0.6 --retry-rate 0.1 \
    --rate-limit 0.5 --output /tmp/redis_bench.json
```

Key flags: `--urls`, `--workers`, `--duration` (cap on the claim/complete
phase), `--retry-rate`, `--domains`, `--priority-distribution` (`fixed:<p>`
/ `uniform:<lo>-<hi>` / `weighted:<p1>:<w1>,<p2>:<w2>,...`), `--process-time`
(simulated per-claim work), `--rate-limit`.

## 2. Multi-process distributed benchmark

Launches `--workers` (1/2/4/8/16, or any int) **independent OS processes**
via `multiprocessing.Process` against one shared Redis namespace — not
asyncio tasks in one process, which would share a GIL/connection pool and
can hide races a real distributed deployment would hit. Detects
cross-process duplicate successful claims, aggregates throughput, and
reports final frontier state.

```bash
python tests/benchmarks/distributed_benchmark.py --workers 4  --urls 5000  --duration 20
python tests/benchmarks/distributed_benchmark.py --workers 16 --urls 20000 --duration 30 --retry-rate 0.1
```

Resource usage in the output includes child-process CPU/RSS (summed across
all worker processes), not just the coordinator's own.

## 3. Crash / recovery test

Deterministic timeline: worker A claims a URL → is never completed (no
`mark_*`, no renewal — simulates `kill -9`) → lease expires → a direct
`reclaim_and_promote()` call (the same thing the real asyncio recovery task
in `core/crawler_manager.py` calls on a timer) reclaims it → a second sweep
promotes it back into its domain queue → worker B claims it → worker A's
stale claim is proven inert → worker B completes it.

```bash
python tests/benchmarks/crash_recovery.py                          # redis, ~2-3s
python tests/benchmarks/crash_recovery.py --lease-ttl 5 --base-backoff 1

# Contrast: the local frontier has no crash-recovery path by design
# (ADR §10) — this shows the claim staying stuck forever instead.
python tests/benchmarks/crash_recovery.py --frontier local
```

Output includes a step-by-step `timeline` with relative timestamps and the
final `get_status_counts()`. Exits non-zero if the Redis scenario doesn't
reach the expected end state.

## 4. Heartbeat endurance test

A synthetic fetch (`asyncio.sleep(--work-duration)`) that intentionally
outlives `--lease-ttl`, run with `core.claim_heartbeat.run_with_heartbeat`
(heartbeat **enabled**) and again with nothing renewing the claim
(**disabled**, the negative control) — a background recovery sweep runs
concurrently in both, on the same timer the real crawler manager uses.

```bash
python tests/benchmarks/heartbeat_endurance.py                                   # runs both, ~5-6s
python tests/benchmarks/heartbeat_endurance.py --work-duration 10 --lease-ttl 3 --recovery-interval 0.5
python tests/benchmarks/heartbeat_endurance.py --mode enabled
```

Expect: enabled → never reclaimed, completes normally; disabled → reclaimed
mid-flight, and the original worker's final completion is silently
rejected as stale. Exits non-zero if either scenario doesn't match that.

## 5. Priority / rate-limit scheduling probe

Seeds a small fixed workload across domains with different priorities
(`--scenario domain:priority:count,...`, default
`urgent:1:2,normal:5:2,bulk:10:1`) and reports the *actual* claim order
with timestamps, so you can see rate-gated domains (temporarily ineligible
right after a claim) get skipped in favor of the next-best eligible domain
rather than blocking the whole queue.

```bash
python tests/benchmarks/priority_ratelimit.py
python tests/benchmarks/priority_ratelimit.py --frontier redis --rate-limit 1.5
python tests/benchmarks/priority_ratelimit.py --scenario fast:1:3,slow:1:3,filler:9:4 --rate-limit 1.0
```

## Resource monitoring

`common.ResourceMonitor` (used by every script) samples, on a background
thread every `--monitor-interval` seconds: process CPU%/RSS (via `psutil`,
already present in this repo's venv — no new dependency), Redis
`used_memory`/`used_cpu_sys`/`used_cpu_user`/`connected_clients` (via
`INFO`, when the target is Redis), and — for the distributed benchmark
only — summed child-process CPU/RSS across all worker processes. Falls back
to fewer fields (never raises) if `psutil` is unavailable.

## What these scripts are *not*

Not a load-test campaign, not a real crawl, not a substitute for
`tests/frontier_test.py` / `tests/redis_frontier_test.py` (the correctness
unit tests already covering the claim/lease/retry contract in isolation).
These scripts are for *manually* comparing backends and *manually*
exploring distributed edge cases at whatever scale and duration you choose
— none of them are wired into CI or run automatically.
