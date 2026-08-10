# Benchmarks

Why benchmarks exist, what conclusions they've already established, and
how to run them. For exact CLI flags of each script, see
[`tests/benchmarks/README.md`](../tests/benchmarks/README.md). For full
historical investigation write-ups (methodology, raw numbers, rejected
hypotheses), see the linked documents under
[`docs/architecture/history/`](architecture/history/).

## Why benchmarks exist

The frontier and Media Evidence are distributed, concurrent systems where
correctness bugs (lost claims, duplicate work, starvation) and performance
regressions are easy to introduce silently. The benchmark scripts in
`tests/benchmarks/` exist to make throughput, latency, fairness, and
crash-recovery behavior measurable and reproducible, separate from the
pytest correctness suite. They are deliberately not pytest-collected
(filenames don't match `test_*.py`/`*_test.py`) — they're manual tools you
run and read the output of, not assertions that pass/fail in CI.

## Local vs. distributed benchmark

- `frontier_benchmark.py` — one process, multiple asyncio-concurrent
  claim/complete threads against either frontier backend. Good for
  frontier-implementation-only throughput and per-operation latency.
- `distributed_benchmark.py` — **N independent OS processes**
  (`multiprocessing.Process`, not asyncio tasks) sharing one Redis
  namespace, deliberately to expose real cross-process races that a
  single-process benchmark can't.

## Redis vs. SQLite

Most benchmark scripts support `--frontier {local,redis}` — "local" here
means the in-process `URLFrontier` (SQL-mode backend), not literally
"localhost Redis." There is no dedicated SQLite-throughput benchmark
beyond this, because SQLite is not a production Redis substitute
([system-architecture.md §15](architecture/system-architecture.md#15-redissqlite-boundary)) —
the local-frontier benchmark exists to validate the development backend
still works correctly and reasonably fast, not to compare it against
Redis as a production alternative.

## Worker scaling

**Established conclusion — do not re-litigate without new evidence.**
`throughput-ceiling-audit.md` measured the Redis frontier's pure
claim/complete ceiling (rate limiting disabled, retries disabled) at 40
domains, 200k URLs, 30s runs:

| Workers | Throughput |
|---|---|
| 2 | ~8,254 URLs/s |
| 4 | ~13,069 URLs/s |
| 8 | ~13,771 URLs/s |
| 16 | ~13,582 URLs/s |

The plateau at 8 workers is caused by single-threaded Redis server CPU
approaching saturation (measured directly, time-normalized: 24.5% → 46.0%
→ 74.5% → 94.6% → 96.1% at 1/2/4/8/16 workers) — not client-side CPU,
which stays well under budget throughout. A realistic 10ms of simulated
per-claim work between claim and completion drops Redis CPU to 5.9% and
throughput becomes exactly `workers × (1/10ms)` — i.e. **real crawl
workloads (network fetch + parse dominate wall time) are nowhere near this
ceiling**, and this is expected to hold in production. Recommendation from
that audit, verbatim: *"~13.4-13.8K URLs/s is good enough. Do not spend
another optimization cycle on the frontier's throughput ceiling."* Full
methodology: [`architecture/history/throughput-ceiling-audit.md`](architecture/history/throughput-ceiling-audit.md).

## Rate-limit vs. no-rate-limit tests

`frontier_benchmark.py`, `distributed_benchmark.py`, and
`domain_starvation.py` all support `--no-rate-limit`/`--rate-limit`. Use
`--no-rate-limit` to measure the frontier's raw implementation ceiling;
use a realistic `--rate-limit` to measure behavior closer to a real
politeness-constrained crawl. These are not interchangeable — a
no-rate-limit run with few domains will also reproduce domain starvation
(see below), which is a fairness bug, not a throughput measurement.

## Resource measurements

`tests/benchmarks/common.py`'s `ResourceMonitor` samples process CPU%/RSS
via `psutil` on a background thread during a run, plus Redis `INFO` fields
when the target is Redis (used to isolate client-side vs. server-side
bottlenecks — see the worker-scaling conclusion above, which depends
entirely on getting this normalization right: an earlier audit
mis-attributed the bottleneck to client CPU because it read cumulative
`used_cpu_sys`/`used_cpu_user` counters as if they were already a
percentage, without a time-normalized delta).

## Crash recovery

`crash_recovery.py` deterministically kills a claim mid-flight and
measures the claim → lease-expiry → reclaim → re-claim timeline, on
either frontier backend. This is what validates
[claim/lease/recovery](architecture/system-architecture.md#14-claim--lease--heartbeat--recovery)
actually works under a simulated crash, not just in the design doc.

## Heartbeat endurance

`heartbeat_endurance.py` runs a synthetic fetch that intentionally exceeds
`lease_ttl`, with heartbeat enabled vs. disabled, to prove
`run_with_heartbeat` (`core/claim_heartbeat.py`) is what prevents a
legitimately-still-working claim from being reclaimed out from under it.

## Priority / rate-limit behavior

`priority_ratelimit.py` reports the actual claim order the frontier
produces across domains with different priorities and rate gates —
useful for validating a scheduling change didn't quietly break priority
ordering or the "skip a rate-gated domain, don't block on it" behavior.

## Known benchmark pitfalls

**Blacklist contamination (fixed).** An earlier diagnostic run
accidentally wrote 20 synthetic benchmark domains
(`bench0.example.test`..`bench19.example.test`) into the real,
gitignored `datasets/domain_blacklist.txt` by calling `add_to_blacklist()`
against the production path instead of an isolated one. Confirmed no
longer present in the current blacklist file. Fixed by
`tests/benchmarks/common.py`'s `isolate_blacklist()` (points `URLUtils` at
a fresh temp file) and `make_domains(..., run_id=...)` (embeds a run id so
synthetic domains can never again collide with real blacklist state) —
both are already called by `frontier_benchmark.py` and
`distributed_benchmark.py`. Full account:
[`architecture/history/benchmark_bug_audit.md`](architecture/history/benchmark_bug_audit.md) /
[`benchmark_bug_fix.md`](architecture/history/benchmark_bug_fix.md).

**Client-side accidental latency dominates naive measurements.** A ~77x
gap between raw Redis Lua round-trip time (0.08ms) and the full
`get_next_url()` call (6.07ms) turned out to be almost entirely a
blacklist-cache bug re-parsing a 1,463-line file on every single frontier
call, not a Redis or frontier-design cost. If a benchmark shows
suspiciously high per-operation latency, profile before concluding it's a
Redis/Lua-script problem — client-side accidental costs have dominated
before. Full account:
[`architecture/history/frontier-optimization-audit.md`](architecture/history/frontier-optimization-audit.md).

## Blacklist isolation

All Redis-backed benchmark scripts default to **db 2**, namespace prefix
`bench*` — isolated from production (db 0) and the pytest Redis suite
(db 1). Blacklist isolation (above) is separate from and in addition to
this Redis isolation — a benchmark can be Redis-isolated and still
poison the real on-disk blacklist file if it doesn't also call
`isolate_blacklist()`.

## Domain starvation

`domain_starvation.py` (7 named scenarios: `finite`,
`rate-limit-skip`, `replenish`, `scan-limit-window`, `retries`,
`multi-worker`, `recovery`) reproduces two distinct starvation mechanisms:
strict-priority starvation (resolved by any `rate_limit > 0`) and
`domain_scan_limit`-window starvation (a domain ranked outside the
top-K `domain_scan_limit` candidates is never examined, regardless of
wait time). See
[system-architecture.md §10](architecture/system-architecture.md#10-url-prioritization)
and
[`architecture/history/domain-starvation-audit.md`](architecture/history/domain-starvation-audit.md)
for the measured reproduction and
[`architecture/history/domain-scan-limit-decision.md`](architecture/history/domain-scan-limit-decision.md)
for why the current limit is 250, not unbounded.

## Interpreting benchmark results

- A throughput number alone is not meaningful without knowing whether
  rate limiting was enabled and how many distinct domains were used —
  both change the bottleneck being measured entirely.
- Redis CPU% (time-normalized delta, not a raw cumulative counter) is the
  right signal for "is Redis itself the bottleneck," not client CPU —
  client CPU has looked misleadingly high in past measurements taken
  before the blacklist-cache fix.
- A regression in `claim_operation_latency` or `completion_operation_latency`
  (the frontier call's own cost) that isn't matched by a regression in
  `queue_wait_latency` (insertion → claim, dominated by queueing not
  frontier cost) points at the Lua script or client library, not at
  scheduling.

## Running benchmarks

```bash
python tests/benchmarks/frontier_benchmark.py --frontier local --urls 10000 --workers 4
python tests/benchmarks/frontier_benchmark.py --frontier redis --urls 20000 --workers 8 --no-rate-limit
python tests/benchmarks/distributed_benchmark.py --workers 8 --urls 20000 --duration 30 --retry-rate 0.1
python tests/benchmarks/crash_recovery.py --frontier redis
python tests/benchmarks/heartbeat_endurance.py --mode both
python tests/benchmarks/priority_ratelimit.py --frontier redis --rate-limit 1.5
python tests/benchmarks/domain_starvation.py replenish --high-count 5 --low-count 3
python tests/benchmarks/media_evidence_benchmark.py --assets 500 --claim-workers 4
```

Full flag reference for every script:
[`tests/benchmarks/README.md`](../tests/benchmarks/README.md).

## Historical run analysis

Real (non-synthetic) nightly-crawl-run performance over time — including
the 81%-throughput-loss regression that originally triggered the entire
optimization-audit chain linked above — is preserved as a historical
log, not current-state documentation:
[`architecture/history/test-analysis.md`](architecture/history/test-analysis.md).
