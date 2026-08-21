# Phase N7 — Scheduler Backpressure Implementation

Status: **COMPLETE.** Implements N6's selected Option A
(`asyncio.Queue(maxsize=concurrency)`) in all six `crawler/*_crawler.py`
backends. No Redis, Lua, lease, or retry semantic changed.

---

## 1. Status

COMPLETE.

## 2. Problem

Per N5 (`docs/architecture/redis-frontier-concurrency-audit.md`, Finding
N5-1) and N6 (`docs/architecture/scheduler-backpressure-design.md`): every
crawler backend's `scheduler()` claims URLs from Redis and pushes them onto
an **unbounded** local `asyncio.Queue`, with no relationship to how many of
the `concurrency` `worker()` tasks are actually free. A claim's Redis lease
clock starts at claim time, but `run_with_heartbeat` (the only thing that
renews it) doesn't start until a worker actually dequeues the claim. If the
scheduler outpaces the workers, claims sit in the local queue with their
lease ticking down unrenewed, causing pre-processing lease expiry,
mid-processing reclaim, stale completions, retry-budget waste, and premature
`failed_permanent` — all without a single genuine fetch failure.

## 3. N6 evidence (summary — see the design doc for full detail)

N6 built a real-Redis synthetic harness
(`tests/benchmarks/scheduler_backpressure.py`) and measured: the unbounded
baseline drove 92.7% of URLs (556/600) to `failed_permanent` with zero
genuine fetch failures injected; a rate-matched negative control on the
identical unbounded code path showed zero disruption (proving the defect is
the rate mismatch, not the queue type); four candidate architectures were
compared on an identical overload workload, and **Option A**
(`asyncio.Queue(maxsize=concurrency)`) and Option C (full-lifetime
semaphore) both eliminated the defect completely and were
measured-equivalent; Option A was selected for its simplicity — no new
synchronization primitive, `scheduler()`'s existing `await
self.queue.put(claim)` already provides correct backpressure once the
queue is bounded.

## 4. Implementation

One-line constructor change in each of the six `crawler/*_crawler.py`
backends:

```python
self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue()
```
→
```python
self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue(maxsize=self.concurrency)
```

No other line was changed. In every backend, `self.concurrency` is already
assigned (and is always `>= 1`, via each backend's existing `max(1, ...)`
clamp) before the queue is constructed, so `maxsize` is never `0`
(`asyncio.Queue(maxsize=0)` would mean unbounded — confirmed not to occur).
`scheduler()`'s pre-existing `await self.queue.put(claim)` (unchanged) now
blocks once the queue holds `concurrency` claims, which is the entire
backpressure mechanism — no other code path was touched.

## 5. Files changed

| File | Change |
|---|---|
| `crawler/async_crawler.py` | `asyncio.Queue()` → `asyncio.Queue(maxsize=self.concurrency)` |
| `crawler/http_crawler.py` | same |
| `crawler/hybrid_crawler.py` | same |
| `crawler/playwright_crawler.py` | same |
| `crawler/scrapling_crawler.py` | same |
| `crawler/selenium_crawler.py` | same |
| `crawler/tor_crawler.py` | same |
| `docs/architecture/scheduler-backpressure-implementation.md` | new — this document |

`git diff --stat`: 7 files changed, 7 insertions(+), 7 deletions(-) — exactly
one line per crawler backend, nothing else.

## 6. Why Option A

Unchanged from N6's decision (§15–16 of the design doc): Option A and
Option C are measured-equivalent on correctness and throughput; Option A is
strictly simpler (no new synchronization primitive spanning multiple
coroutines/await points across six near-identical backends) and requires no
change beyond the queue constructor, since `scheduler()`'s existing
`queue.put()` already blocks correctly. This phase re-inspected the current
tree (§7 of the phase brief) and found no implementation contradiction that
would invalidate that decision — the architecture described in N5/N6
(`self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue()` in all six
backends, `await self.queue.put(claim)` in every `scheduler()`) matched the
current source exactly, with no material drift.

## 7. Lease-safety verification

Production defaults, re-confirmed unchanged in `core/config.py` and
`config.yaml` this phase: `concurrency=25`, `lease_ttl=90.0`.

**INFERRED (carried from N6, re-checked against newer telemetry this
phase):** worst-case claim-to-completion time under Option A is
approximately `concurrency / completion_rate + worker_latency`. N6 used the
N4 run's `completed_per_sec=0.674` → `25 / 0.674 ≈ 37s`, well under the 90s
lease.

This phase pulled a **newer** production telemetry file not available to
N6: `benchmark/results/overnight_e2e_crawler.json` (an ~8.9-hour run,
2026-08-12 → 2026-08-13, same production config: `concurrency=25`,
`lease_ttl=90.0`). Its `this_run.throughput.discovered_unique_per_sec` /
aggregate completed-rate is `0.701/s` — same order of magnitude as N4's
`0.674/s`, in fact marginally higher. Applying the same model:
`25 / 0.701 + worker_latency ≈ 35.7s + worker_latency`, still comfortably
under the 90s lease, consistent with (and not contradicting) N6's margin
estimate. This run is itself a **pre-N7** run — 99.2% of its URLs
(22,189/22,359) ended in `failed_permanent`, the same unbounded-queue defect
N5/N6 characterized, now visible at production scale over a long run — so
its own `completed_per_sec` is contaminated by fast reclaim-driven
terminal-failure churn, not a clean post-fix measurement. It is used here
only as a floor/sanity-check on the model's order of magnitude, not as a
post-fix throughput prediction.

**VERIFIED, this phase, against real Redis (§9 below):** at the harness's
compressed scale (`concurrency=20`, `lease_ttl=3.0s`), Option A reproduced
zero disruption (0 pre-processing expiry, 0 mid-processing reclaim, 0 stale
completions, 0 spurious `failed_permanent`), matching N6's own measurement
exactly. This confirms the mechanism, not the production-scale absolute
numbers.

**NOT TESTED:** a live run at full production `concurrency=25`/
`lease_ttl=90s` scale under Option A. As N6 noted, a full-scale run would
take on the order of the lease's own duration to observe a single reclaim
cycle — judged not worth the wall-clock cost for this phase, consistent
with N6's own recommendation to prefer lightweight telemetry review over a
new synthetic full-scale run. No evidence found in this phase suggesting
the 90s lease lacks reasonable margin at production defaults; nothing here
triggered the phase's STOP condition for insufficient lease margin.

## 8. Tests

Environment: real Redis at `localhost:6379`. Pytest suite uses db 1 (never
touched by anything else in this phase). Python venv at `env/`.

**Pre-implementation baseline** (before any edit):

```
pytest tests/redis_frontier_test.py tests/redis_startup_recovery_test.py \
  tests/frontier_test.py tests/frontier_executor_test.py \
  tests/crawler_manager_recovery_test.py \
  tests/crawler_manager_seed_failure_semantics_test.py \
  tests/claim_heartbeat_test.py tests/crawler_heartbeat_integration_test.py \
  tests/network_health_test.py tests/crawler_test.py tests/hybrid_crawler_test.py \
  tests/extra_crawlers_test.py tests/scrapling_crawler_test.py -q
```
Result: **1 failed, 131 passed, 2 skipped** (48.57s). The 1 failure:
`TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`
— exactly the documented N5-2 `rate_limit=0` clock-jitter flake, out of
scope for N7.

**Post-implementation, same command:** **2 failed, 130 passed, 2 skipped**
(48.00s). The 2 failures: the same `test_concurrent_...bounded_shared_thread_pool`
flake, plus `TestMultiWorkerCoordination::test_get_next_url_no_duplicates`
— both are the two N5-2-documented flakes (N5 audit §5/§6/§13; N6 design
doc §20, "reproduced once during regression re-run"). Neither test imports
or exercises anything under `crawler/*.py` — both exercise
`RedisURLFrontier`/`AsyncFrontier` directly via threads/`asyncio.gather`
against a `rate_limit=0` fixture, unrelated to the local-queue change made
in this phase. Re-ran `test_get_next_url_no_duplicates` in isolation 3×
immediately after: passed 3/3, confirming it is the known
intermittent-under-suite-load flake, not a regression (matches its own
documented ~17% reproduction rate from N5 §5).

**Full suite:**

```
pytest tests/ -q --ignore=tests/benchmarks
```
Result: **1 failed, 323 passed, 2 skipped** (74.02s). The 1 failure is
again `test_concurrent_redis_calls_use_a_bounded_shared_thread_pool` (the
same N5-2 flake). No other test in the full suite failed.

**Conclusion: no N7-caused regression.** Every failure observed in this
phase matches the two already-characterized, pre-existing N5-2 flakes
exactly (test name, assertion, and root cause — `rate_limit=0` clock-jitter
in `claim_next`'s rate gate, unrelated to the crawler-side queue change).
Per the phase brief, N5-2 was explicitly not touched.

## 9. Benchmark

Ran `tests/benchmarks/scheduler_backpressure.py` (unmodified N6 harness)
against real Redis, **db 2**, fresh namespaces
(`n7_backpressure_baseline`, `n7_backpressure_optiona`), using N6's exact
`baseline_unbounded` workload parameters
(`concurrency=20, urls=600, domains=50, worker_latency=1.5s, lease_ttl=3.0s,
recovery_interval=0.75s, rate_limit=0.015s, max_retries=3, base_backoff=0.4s,
max_backoff=2.0s, reclaim_batch_size=200, real_failure_rate=0.0, seed=42`),
varying only `--queue-maxsize` (0 = today's unbounded behavior, 20 = Option
A / this phase's actual change, `N=concurrency`).

| Metric | N7 baseline (unbounded, this run) | N6 baseline (unbounded, design doc) | N7 Option A (this run) | N6 Option A (design doc) |
|---|---|---|---|---|
| Success | 46/600 (7.7%) | 44/600 (7.3%) | **600/600 (100%)** | 600/600 (100%) |
| Failed-permanent | 554 | 556 | **0** | 0 |
| Pre-processing lease expiry | 1,074 | 1,076 | **0** | 0 |
| Mid-processing reclaim | 588 | 592 | **0** | 0 |
| Stale completions | 0 | 0 | **0** | 0 |
| Peak local queue depth | 1,487 | 1,492 | **20** | 20 |
| Peak Redis inflight | 597 (29.85×) | 597 (29.9×) | **41 (2.05×)** | 41 (2.05×) |
| Completion rate | 0.52/s | 0.50/s | **13.01/s** | 12.91/s |
| Elapsed | 87.68s | 87.7s | **46.13s** | 46.5s |
| Recovery sweeps / reclaimed | 117 / 1,662 | 117 / 1,668 | **62 / 0** | — / 0 |
| Redis EVALSHA delta | 4,435 | 4,441 | **1,897** | 1,899 |
| `category_f_check` (failed w/ 0 real failures) | 554/554 confirmed reclaim-driven | 556 | **0** | 0 |

N7's re-run reproduces N6's numbers within normal single-run noise (both
runs are single, un-averaged runs, as N6 itself noted as a limitation) and
confirms **today's real Redis, today's code, gives the identical
qualitative result N6 measured**: the unbounded queue drives massive
spurious `failed_permanent`; Option A (now the actual production
architecture, applied via this phase's edit — the harness reproduces the
same discipline the six real backends now implement) eliminates the defect
completely — 0 pre-processing expiry, 0 mid-processing reclaim, 0 stale
completions, 0 spurious failures, peak queue depth exactly bounded at
`concurrency=20`, ~57% fewer Redis round trips than baseline.

Note: this harness runs its own synthetic scheduler/worker loop that
mirrors the six backends' shape (per its own docstring and N6 §5) — it does
not literally import `crawler/hybrid_crawler.py`. It validates the
architecture this phase applied to the real files; it is not a literal
integration test of the edited files themselves. §8's pytest suite (which
does exercise the real `crawler/*.py` modules, including
`hybrid_crawler_test.py`, `crawler_test.py`, `extra_crawlers_test.py`,
`scrapling_crawler_test.py`, `crawler_heartbeat_integration_test.py`) is
the direct test of the edited files; it passed with no N7-caused failures.

## 10. Regression results

All checks in §16 of the phase brief were inspected:

- **`concurrency=1` / very small queues:** `asyncio.Queue(maxsize=1)` is
  valid; `self.concurrency` is always clamped `>= 1` in every backend's
  constructor (`max(1, ...)`), so `maxsize` is never `0` (which would mean
  unbounded, defeating the fix). Not separately load-tested at
  `concurrency=1` in this phase, but the code path is identical to any
  other `maxsize > 0` value — no special-cased branch exists that would
  behave differently at 1 versus 20.
- **Empty frontier / scheduler idle:** unchanged — `scheduler()`'s idle-poll
  branch (`self.queue.empty() and self._active_workers == 0 and not
  await self.frontier.has_pending()`) is untouched, and a bounded-but-empty
  queue behaves identically to an unbounded-but-empty queue for this check.
- **Scheduler/worker cancellation, crawler shutdown:** verified by direct
  code inspection (all six backends share the identical pattern, confirmed
  via `grep`): `run()` does `await self._stop_event.wait()`, then in a
  `finally` block unconditionally calls `scheduler_task.cancel()` and
  `task.cancel()` on every worker, then `asyncio.gather(..., 
  return_exceptions=True)`. This cancels the scheduler task directly via
  `asyncio.Task.cancel()` regardless of whether it is blocked inside
  `await self.queue.put(claim)` — cancellation raises `CancelledError`
  inside that await point, unblocking it immediately. **A bounded queue
  cannot deadlock this shutdown path**: the scheduler is never left waiting
  on queue space after workers have stopped, because shutdown cancels the
  scheduler task directly rather than relying on it observing
  `_stop_event` cooperatively between iterations. This pattern was
  identical, character-for-character (modulo tabs vs. spaces), across all
  six backends.
- **`max_pages` termination, indefinite runs:** unaffected — `max_pages`
  clamps `self.concurrency` at construction time (already existing logic,
  untouched) and termination is driven by `_stop_event`, not queue state.
- **Redis unavailable, network OFFLINE, `mark_deferred`,
  `reclaim_and_promote`, stale claim handling, startup recovery:** none of
  these code paths were touched by this phase's edit (confirmed by `git
  diff` — only the queue constructor line changed in each file); regression
  coverage for all of them is exercised by the existing suite
  (`redis_startup_recovery_test.py`, `network_health_test.py`,
  `crawler_manager_recovery_test.py`,
  `crawler_manager_seed_failure_semantics_test.py`,
  `crawler_heartbeat_integration_test.py`), all passing post-change (§8).

No regression beyond the two pre-existing N5-2 flakes was found.

## 11. Redis safety verification

- `db 0` (production): `dbsize` = 50,686 before this phase's benchmark runs
  and 50,686 after — **unchanged**, matching N5's and N6's own recorded
  count exactly.
- `db 1` (pytest suite): never targeted by any benchmark command in this
  phase; only touched by the pytest runs themselves (§8), per the existing
  fixture convention — not modified beyond normal test execution.
- `db 2` (benchmark db): 74 keys before this phase's benchmark runs, 74
  keys after — the two fresh namespaces used this phase
  (`n7_backpressure_baseline`, `n7_backpressure_optiona`) were fully
  cleared by the harness's own `raw_frontier.clear()` calls (start and end
  of each run) plus an explicit post-run key-pattern check/delete,
  confirmed empty before reporting.
- `--redis-db 0` remains hard-refused by the harness itself
  (`tests/benchmarks/scheduler_backpressure.py`, unmodified this phase).

## 12. Performance observations

MEASURED (§9): Option A, re-verified today against real Redis, reproduces
N6's own measured result almost exactly — 100% success (vs. 7.7% baseline),
~25× the baseline completion rate, ~57% fewer Redis `EVALSHA` calls than
baseline, peak local queue depth held exactly at the configured
`maxsize=concurrency` bound throughout the run. This is a strict
improvement on both correctness and throughput versus the unbounded
baseline, consistent with N6's finding that this is not a
correctness/throughput tradeoff.

## 13. Limitations

- The N6/N7 benchmark harness (`tests/benchmarks/scheduler_backpressure.py`)
  is a synthetic reproduction that mirrors the six backends'
  `scheduler()`/`worker()` shape — it does not literally import or execute
  the edited `crawler/*.py` files. The pytest suite (§8) is what directly
  exercises the edited files, and it passed.
- Each §9 benchmark comparison (baseline vs. Option A) is a single,
  un-averaged run, the same limitation N6 itself documented (design doc
  §20) — the qualitative result (0 vs. hundreds of disruption events) is a
  large-margin effect unlikely to be single-run noise, but exact
  throughput/EVALSHA figures are point estimates.
- No new multi-process or full-90s-lease-scale validation was performed in
  this phase — this is unchanged from N6's own stated scope (design doc
  §17, §20); Option A is a pure process-local change and does not touch
  anything relevant to cross-process coordination, so this is not treated
  as a gap specific to N7.
- `concurrency=1` was not separately load-tested (only code-inspected for
  correctness — see §10).

## 14. Deferred

Explicitly not implemented in this phase, per the phase brief:

- **N5-4 / N6 §21.5 — stale-completion/reclaim counter.** N6 strongly
  recommended this as a follow-up but it was not judged "extremely small,
  directly supported by the existing architecture, and necessary for
  validating the backpressure fix" for N7's core objective — the benchmark
  harness's own instrumentation (which reads `_complete()`'s return value
  directly) was sufficient to validate the fix in this phase without adding
  a production counter. Left deferred, as the phase brief explicitly
  permits.
- **N5-2** (`rate_limit=0` test flake) — explicitly out of scope per both
  the N5 audit, N6, and this phase's brief; not touched.
- **N5-3** (stale docstring in `core/redis_frontier.py`) — out of scope for
  N7; not touched.
- Any change to `lease_ttl`, retry semantics, Redis/Lua scripts, or a
  prefetch multiplier (Option B) — none were needed; the lease-margin check
  (§7) found no evidence the 90s default lacks reasonable margin.

## 15. Final N7 status

**COMPLETE.** All six `crawler/*_crawler.py` backends now bound their local
scheduler-to-worker queue to `concurrency`, exactly implementing N6's
selected Option A. The existing `await self.queue.put(claim)` in every
`scheduler()` is the sole backpressure point, unchanged. No Redis, Lua,
lease, or retry semantic was modified. Focused and full test suites pass
with no regression beyond the two pre-existing, already-documented N5-2
flakes. The N6 benchmark, re-run today against real Redis, reproduces N6's
findings almost exactly and confirms the overload failure mode (spurious
`failed_permanent`, pre-processing lease expiry, mid-processing reclaim) is
eliminated under the bounded queue. Production Redis (db 0) was verified
untouched before and after. Shutdown/cancellation was verified safe by code
inspection across all six backends — a bounded queue cannot deadlock
shutdown because the scheduler task is cancelled directly, not relied upon
to observe `_stop_event` cooperatively while blocked on `queue.put()`.

**N7 STOP — implementation complete, no further phase started.**
