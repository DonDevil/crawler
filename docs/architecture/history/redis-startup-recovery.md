# Redis Startup Recovery

Status: **implemented**. Follow-up to
[`redis-startup-recovery-audit.md`](redis-startup-recovery-audit.md), which
established this design without changing code. That document remains the
historical investigation record (concurrency-safety proof, `reclaim_and_promote`
mechanics, the incident that motivated this work); this document describes
what was actually built.

## Previous startup behavior

`CrawlerManager.run()` called `prepare_frontier()`, created the
`_recovery_loop` background task, then immediately `await self._crawler.run()`.
`_recovery_loop`'s first `reclaim_and_promote()` call happened at the next
event-loop yield with no `sleep` before it -- in practice close to concurrent
with worker startup, but not ordered relative to it: nothing prevented a
worker from calling `get_next_url()` before that first sweep completed, and a
single sweep is not guaranteed to fully drain a large backlog (see audit §A).
If no crawler process was running at all (the motivating incident: an
overnight run killed by a crashed dev environment), abandoned inflight claims
and due retries sat un-reconciled in Redis until some process's `run()`
eventually started -- at which point recovery raced worker startup instead of
preceding it.

## New startup ordering

```
CrawlerManager.run()
    |
    prepare_frontier()          (sync, seeds/resumes -- unchanged)
    |
    _run_startup_recovery()     (new -- one-shot, awaited to completion)
    |
    start _recovery_loop task   (unchanged -- periodic, mid-run)
    |
    await self._crawler.run()   (workers may now claim)
```

`_run_startup_recovery()` (`core/crawler_manager.py`) is awaited directly in
`run()`, strictly before the `_recovery_loop` task is created and strictly
before `self._crawler.run()` -- so no worker's `get_next_url()` can execute
before the sweep has either converged or hit its bound. Ordering relative to
`prepare_frontier()` is not correctness-sensitive (seeding only touches
`known`/domain-queue/`meta`/`seq`; recovery only touches
`inflight`/`retry_scheduled`/domain-queue-via-promotion) but running recovery
first reads more naturally: reconcile what's already there, then add new
work.

Gated identically to `_recovery_loop`'s existing task-creation check:
`frontier_config.recovery_enabled and hasattr(self.frontier,
"reclaim_and_promote")`. For the local SQLite frontier this is always a
no-op and returns immediately -- it promotes due retries lazily inside its
own `get_next_url()` (ADR §10) and has nothing for a startup sweep to
reconcile. No SQLite-specific recovery path was added; per the task brief,
the local frontier does not gain a fake Redis-style sweep.

## Bounded recovery policy

`_run_startup_recovery()` loops calling
`self.frontier.reclaim_and_promote(batch_size)` (via `self.async_frontier`,
same offload boundary `_recovery_loop` already uses) until **either**:

- it converges (`(0, 0)` returned -- no more inflight/retry work found), or
- `startup_recovery_max_passes` calls have been made, or
- `startup_recovery_max_duration` seconds have elapsed since the sweep
  started

whichever comes first. Both bounds are checked every iteration (the duration
check runs before each call, so it can never be exceeded by more than one
call's latency); reaching either one stops the sweep and lets startup
continue rather than blocking indefinitely.

### Defaults and why they're safe

Added to `FrontierConfig` (`core/config.py`), alongside the existing
`recovery_enabled`/`recovery_interval`/`reclaim_batch_size` knobs the startup
sweep reuses rather than duplicating:

```python
startup_recovery_max_passes: int = 50
startup_recovery_max_duration: float = 30.0
```

- **`reclaim_batch_size`** (existing, default 200) is reused unchanged --
  the startup sweep does not get its own batch-size knob.
- **`startup_recovery_max_passes = 50`**: the incident that motivated this
  work left 228 inflight claims and 73 retry-scheduled URLs; at the default
  batch size of 200 that backlog fully converges in 2-3 passes (audit §A).
  50 passes gives roughly 15-20x headroom over the worst backlog actually
  observed, while still bounding worst-case work at `50 x 200 = 10,000`
  reconciled entries per startup -- large enough not to be a practical
  limit for a single crashed process's backlog, small enough that a
  pathological/adversarial continuously-refreshed namespace (many other
  systems expiring claims throughout our sweep) cannot turn startup into an
  unbounded loop.
- **`startup_recovery_max_duration = 30.0`** seconds: an independent
  wall-clock ceiling, not derived from pass count, so slow Redis round trips
  (not just call count) are also bounded. 30s is small relative to a crawl
  run's overall lifetime but large enough that even a full 50-pass sweep
  under realistic Redis latency finishes comfortably inside it under normal
  conditions.

Both bounds are simple integers/floats on the existing `FrontierConfig`
model -- no new configuration subsystem was introduced.

## Multi-system concurrency semantics

Unchanged from the audit's conclusion, because nothing about the concurrency
story required new code: `reclaim_and_promote` is one atomic Lua script, so
two callers -- this process's own startup sweep, another independent
system's startup sweep, or any periodic loop, all sharing the same Redis
namespace -- can never observe or reclaim the same URL. No leader election,
distributed lock, single recovery owner, or new coordinator service was
added or is needed. `test_concurrent_startup_recovery_is_safe` (below)
demonstrates two independent `CrawlerManager`/`RedisURLFrontier` pairs
running their startup sweeps concurrently against one abandoned claim: it is
reclaimed exactly once and becomes claimable by exactly one of the two
systems.

## Interaction with periodic recovery

`_recovery_loop` is unchanged -- same code, same cadence
(`recovery_interval`), same broad `except Exception` handling per iteration.
The startup sweep is a separate, one-shot mechanism sharing only the
underlying `reclaim_and_promote` primitive:

- The startup sweep reconciles whatever a *previous* process (possibly the
  only one that ever ran) left behind, once, before this process's workers
  start.
- `_recovery_loop` keeps running for the lifetime of this run, reconciling
  crashes that happen *during* it (e.g. one worker dying mid-run while the
  rest of the system stays up).

`test_periodic_recovery_still_runs` confirms the periodic task is still
created and still fires multiple times after a startup sweep has already
run immediately before it -- the sweep does not disable or replace the
periodic loop.

## Lease/heartbeat behavior

Unchanged. The startup sweep calls the exact same `reclaim_and_promote`
Lua script the periodic loop already calls, with the identical
lease-expiry precondition (`ZRANGEBYSCORE inflight -inf now`) -- a claim
whose lease has not yet lapsed is structurally invisible to it, regardless
of caller. `claim_heartbeat.py`'s renewal cadence (`lease_ttl / 3`) is
untouched, so a genuinely live worker's claim is never reclaimed by a
startup sweep any more than by the periodic loop. `lease_ttl` itself was
not changed.

## Retry / priority / rate-limit behavior

Unchanged -- the startup sweep is a caller of the existing primitive, not a
redesign of it. Recovered URLs retain their original domain and priority
(read from `meta:{url}`, never mutated by recovery) and attempt continuity
(the `attempts:{url}` counter is untouched by reclaim except at terminal
finalization). A recovered retry re-enters its domain queue with a fresh
sequence number, landing at the back of its priority tier rather than its
exact original position -- pre-existing, expected `reclaim_and_promote`
behavior (audit §H), not something startup recovery changes. Domain rate
limiting (`domain:{domain}:next_time`) is untouched by either phase of
`reclaim_and_promote`.

## Error handling

A `FrontierUnavailable` raised by `reclaim_and_promote` (the existing Redis
failure contract -- see
[`frontier-redis-failure-semantics.md`](frontier-redis-failure-semantics.md)
§3) is **not** caught inside `_run_startup_recovery()`. It propagates out of
`run()` uncaught (the startup sweep runs before `run()`'s
`try/except`/`finally` block that wraps `self._crawler.run()`), so a Redis
infrastructure failure during the sweep fails the crawler startup loudly
instead of silently letting workers begin claiming against state that was
never actually reconciled. This mirrors `prepare_frontier()`'s existing
behavior one line above it (also unguarded at the `run()` level) rather than
inventing a new fallback policy. It intentionally differs from
`_recovery_loop`'s handling of the same exception (broad `except Exception`,
logged, retried next tick) because the two mechanisms have different
failure requirements: the periodic loop is long-running and a missed tick
is harmless (the next tick retries), while the one-shot startup gate exists
specifically so workers never start against unreconciled state -- catching
and continuing here would defeat the sweep's entire purpose.

## Logging

One summary line when the sweep finishes (`logger.info`, matching the
example in the task brief):

```
Redis startup recovery: passes=2 reclaimed=228 requeued=73 elapsed=0.04s bound_reached=false
```

No per-URL logging was added, consistent with `_recovery_loop`'s existing
`logger.debug` summary-only pattern.

## Tests

New file: `tests/redis_startup_recovery_test.py` (10 tests, Redis DB 1, same
convention as `tests/redis_frontier_test.py` and
`tests/crawler_manager_recovery_test.py`). CrawlerManager-level integration
tests only -- `reclaim_and_promote`'s own Lua-level correctness is already
covered by `tests/redis_frontier_test.py::TestClaimLifecycle` and was not
re-proven here.

| Test | Proves |
|---|---|
| `test_startup_recovery_recovers_expired_claim` | A claim abandoned by a simulated previous process is reconciled (`inflight` -> `queued`) by a fresh `CrawlerManager`'s startup sweep |
| `test_startup_recovery_does_not_reclaim_live_claim` | A claim whose lease has not expired is left byte-for-byte untouched |
| `test_startup_recovery_precedes_worker_claiming` | Instruments both `reclaim_and_promote` and the stubbed crawler's first `get_next_url()`; asserts strict ordering |
| `test_startup_recovery_handles_batch_backlog` | 7 expired claims at `batch_size=3` require multiple passes; sweep converges and reconciles all 7 |
| `test_startup_recovery_handles_due_retry` | A due `retry_scheduled` entry is promoted back to `queued` with attempt continuity |
| `test_concurrent_startup_recovery_is_safe` | Two independent `CrawlerManager`/`RedisURLFrontier` pairs run startup sweeps concurrently (`asyncio.gather`) against one abandoned claim; it is reclaimed exactly once and claimable by exactly one system |
| `test_recovered_attempt_and_fencing` | Attempt increments 1 -> 2 across a recovery; the stale attempt-1 claim's `mark_visited` is rejected as a no-op; the new claim completes normally |
| `test_startup_recovery_preserves_existing_queued_work` | Ordinary queued/visited URLs are unaffected; only the genuinely abandoned claim is touched |
| `test_periodic_recovery_still_runs` | The periodic `_recovery_loop` task is still created and still fires multiple times after a startup sweep ran immediately before it |
| `test_startup_recovery_bound` | A `reclaim_and_promote` that never returns `(0, 0)` is stopped at exactly `startup_recovery_max_passes` calls |

### Test results

```
tests/redis_startup_recovery_test.py                              10 passed
tests/crawler_manager_recovery_test.py                              5 passed
tests/redis_frontier_test.py                                       16 passed
tests/crawler_manager_seed_failure_semantics_test.py                 8 passed
```

No regressions in any of the existing suites touched by this change.

## Limitations

- **Worst-case startup latency is bounded but non-zero.** A pathological
  namespace (many independent systems continuously expiring claims) can
  make the sweep run for the full `startup_recovery_max_duration` (30s
  default) before yielding to worker startup. This is the intended
  trade-off the bound exists to make explicit, not an oversight.
- **The circuit-breaker-free error path means one Redis blip during the
  sweep aborts the whole startup**, not just that one pass, per the "do not
  invent a new fallback policy" instruction -- an operator restarting the
  process is the expected recovery path, matching how `prepare_frontier()`
  already behaves on an unguarded failure.
- **No dedicated duration-bound test.** `test_startup_recovery_bound`
  exercises the pass-count bound (deterministic and fast); the duration
  bound uses the identical stop-condition code path but was not separately
  exercised with a real 30s wait, to keep the suite fast. Both bounds share
  one loop body, so this is a coverage gap in redundancy only, not in the
  underlying mechanism.
- **`reclaim_and_promote`'s own known asymmetries are unchanged and
  out of scope**, per the task brief: the `failed_permanent` branch's
  immediate `meta:{url}` deletion (ignoring `terminal_meta_ttl_seconds`)
  and phase (b)'s silent-drop-if-`meta`-missing edge case, both noted in
  the audit §F and not touched here.
