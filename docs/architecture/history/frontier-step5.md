# Frontier Migration — Step 5 Implementation Notes

Status: **implemented**. Corresponds to `docs/architecture/frontier-adr.md` §13, migration
step 5 only:

> 5. Add `renew_claim` + the shared `run_with_heartbeat` helper (§8), thread it through all 6
>    worker loops.

Read `docs/architecture/frontier-adr.md`, `docs/architecture/frontier-step1.md`,
`docs/architecture/frontier-step2.md`, `docs/architecture/frontier-step3.md`, and
`docs/architecture/frontier-step4.md` in full before making any changes. No re-audit of the
rest of the repository was performed.

Objective: prevent a legitimately slow crawler operation (slow Tor circuit, JS-heavy page,
deliberately throttled target) from losing its Redis claim to the Step 4 recovery task, while
still letting that same recovery task reclaim a genuinely crashed worker's claim promptly.

## Files changed

- **`core/claim_heartbeat.py`** (new) — `ClaimLostError`, `default_heartbeat_interval`,
  `resolve_heartbeat_interval`, `run_with_heartbeat`. The one shared mechanism every backend
  wraps its fetch call with, per ADR §8's explicit "implement once, shared, not duplicated"
  guidance.
- **`crawler/{async,http,tor,playwright,selenium,scrapling}_crawler.py`** — each backend's
  `__init__` gained a `heartbeat_interval: Optional[float] = None` parameter and computes
  `self.heartbeat_interval` via `resolve_heartbeat_interval`, reading the actual wrapped
  frontier's `.lease_ttl` attribute. Each `worker()`'s `html, failure_reason = await
  self.fetch(...)` call became `(html, failure_reason), claim = await run_with_heartbeat(self.frontier,
  claim, self.fetch(...), self.heartbeat_interval)`, and a new `except ClaimLostError:` clause
  was added ahead of the existing `except Exception:` clause.
- **`crawler/hybrid_crawler.py`** — same constructor change, plus `heartbeat_interval` added to
  `common_args` so the six sub-engines get a consistent value. The engine-escalation `while
  plan:` loop (previously inline in `worker()`) was extracted into a new `_run_engine_plan(self,
  url)` method returning `(html, failure_reason, attempt_chain, engine_used)`; `worker()` now
  wraps a single call to that method with `run_with_heartbeat`. This was necessary, not
  cosmetic: a claim can span several engine attempts before a final outcome is known (ADR §11's
  note on `hybrid_crawler.py`'s escalation logic), so the heartbeat has to cover the *whole*
  chain, not just the first engine's fetch.
- **`core/config.py`** — `FrontierConfig` gained `heartbeat_interval: Optional[float] = None`.
  `None` (the default) means "derive automatically from `lease_ttl`"; a numeric value is an
  explicit override, still clamped below `lease_ttl` (see "Configuration" below).
- **`core/crawler_manager.py`** — `crawler_args` gained `"heartbeat_interval":
  frontier_config.heartbeat_interval`, flowing the (possibly `None`) override into whichever
  backend(s) get constructed.
- **`config.yaml`** — documents the new `heartbeat_interval` knob under `crawler.frontier`,
  left commented out (auto-derive) by default.
- **`tests/claim_heartbeat_test.py`** (new) — unit tests for `core/claim_heartbeat.py` against
  both frontier backends.
- **`tests/crawler_heartbeat_integration_test.py`** (new) — integration tests proving the
  wiring inside `AsyncCrawler.worker()` behaves correctly end to end.
- **`tests/hybrid_crawler_test.py`** — one new test proving the heartbeat spans the full
  engine-escalation chain.

Not touched, per the task's explicit scope: `core/frontier.py` (the `Frontier` protocol is
still fully synchronous — requirement 6), `core/url_frontier.py`, `core/redis_frontier.py`
(no correctness problem in the Redis keyspace/Lua scripts required touching them — requirement
17), `core/frontier_executor.py` (used as-is — requirement 4/5), the fingerprinter (requirement
19), any fetch/parsing/retry/priority/rate-limit logic (requirement 16).

## Heartbeat design

### Lifecycle

`run_with_heartbeat(frontier, claim, coro, heartbeat_interval)`:

```python
task = asyncio.ensure_future(coro)
try:
    while True:
        done, _ = await asyncio.wait({task}, timeout=heartbeat_interval)
        if task in done:
            return task.result(), claim

        renewed = await frontier.renew_claim(claim)
        if renewed is None:
            raise ClaimLostError(claim)
        claim = renewed
except BaseException:
    await _cancel_and_drain(task)
    raise
```

- **Starts only after a claim exists.** Every call site is inside `worker()`, after the
  `claim is None` and blacklist checks — never before `get_next_url()` has actually returned a
  claim (requirement 9).
- **Wraps exactly the in-flight work**, not a fixed duration: `coro` is the same
  `self.fetch(...)` call (or, for `HybridCrawler`, the whole `_run_engine_plan(url)`
  escalation chain) that used to be awaited directly. Whichever finishes first — the work or a
  failed renewal — ends the loop.
- **Stops on every exit path.** The `except BaseException:` clause (not just
  `except asyncio.CancelledError:`) covers three cases uniformly: `ClaimLostError` raised by
  this function itself, this function being cancelled from outside (worker shutdown), and any
  unexpected exception out of `renew_claim` — on all three, the wrapped work task is cancelled
  and drained *before* the exception propagates. A clean return (`task in done`) needs no
  cleanup since there's nothing left running. This is what satisfies requirements 10 and 11: no
  heartbeat-adjacent task can outlive the call, on any path.
- Returns `(result, claim)` with `claim` reflecting the most recent successful renewal (fresh
  `lease_expires_at`); every call site rebinds its local `claim` variable from this, so a
  subsequent `mark_visited`/`mark_failed` uses the latest claim (though `mark_*`'s token check
  doesn't actually change across a renewal — only `lease_expires_at` does, so this is
  correctness-hygiene, not a requirement).

### Interval calculation

```python
def default_heartbeat_interval(lease_ttl):
    return max(_MIN_HEARTBEAT_INTERVAL, lease_ttl / 3.0)   # _MIN_HEARTBEAT_INTERVAL = 0.05

def resolve_heartbeat_interval(configured, lease_ttl):
    if configured is None or configured <= 0:
        return default_heartbeat_interval(lease_ttl)
    return min(configured, max(_MIN_HEARTBEAT_INTERVAL, lease_ttl / 2.0))
```

- **Default**: `lease_ttl / 3`. A live worker gets two renewal attempts of margin before its
  lease could actually lapse — one missed or slow tick (network hiccup, scheduling jitter under
  load) still leaves time for the next to land.
- **Explicit override is always clamped below `lease_ttl`** (`lease_ttl / 2` ceiling): a
  configured interval that reached or exceeded the lease would guarantee the lease expires
  before the first renewal could ever fire, silently defeating the whole mechanism. This is
  corrected rather than honored (requirement 13).
- **Each backend derives its interval from the frontier instance it actually holds**, not from
  a separately-threaded config value: `resolve_heartbeat_interval(heartbeat_interval,
  getattr(getattr(frontier, "raw", frontier), "lease_ttl", None))`, reading `.lease_ttl` off the
  concrete `URLFrontier`/`RedisURLFrontier` object. This means the interval can never drift out
  of sync with whatever `lease_ttl` that frontier was actually constructed with — including in
  tests that construct a frontier directly with a custom `lease_ttl` and never go through
  `CrawlerManager`/`FrontierConfig` at all.
- A small absolute floor (`0.05s`, not the more obvious `1.0s`) guards only against degenerate
  input (`lease_ttl <= 0`). It's deliberately small: a naive 1-second-scale floor would silently
  override a legitimately short `lease_ttl` (an operator choosing a fast-detection lease, or any
  test), forcing the interval past the lease itself and defeating the mechanism exactly the way
  an unclamped override would. `default_heartbeat_interval` is where the production-sane
  default lives; this floor only exists so `run_with_heartbeat` can't divide-by-zero or busy-loop
  on bad input.

### Stale-claim behavior

If `renew_claim` returns `None` (Redis: the claim was reclaimed by the Step 4 recovery sweep or
completed elsewhere; local: the claim was completed under a different token), `run_with_heartbeat`
raises `ClaimLostError(claim)` after cancelling and draining the in-flight work task. Every
`worker()` catches this ahead of the generic `except Exception:`:

```python
except ClaimLostError:
    logger.warning(f"Claim lost for {url}: lease was reclaimed before this worker "
                    "finished ...; abandoning without marking completion")
```

Deliberately **no** `mark_visited`/`mark_failed`/`mark_skipped` call in that branch — this is
requirement 12's "define and implement the correct behavior explicitly." The claim is no longer
this worker's to resolve: on Redis, its token no longer matches the live `claim:{url}` record
(a `mark_*` call would be silently rejected as a no-op anyway per the ADR's stale-claim
guarantee — this is a second, correctness-hygiene reason rather than a strict requirement,
since a call would have been harmless), and being explicit about *not* calling it avoids ever
depending on that no-op behavior for correctness. `_pages_crawled`/`_pages_failed` are not
incremented either, since this worker didn't actually process the URL to a conclusion — a
different worker (or a future retry, if the reclaim went to `retry_scheduled`) owns that
outcome now.

### Task cancellation

`_cancel_and_drain(task)` is the single place that cancels the wrapped work: `if task.done():
return`, else `task.cancel()` followed by `await task` inside a `try/except BaseException: pass`
(only to avoid an orphaned-task/"exception never retrieved" warning — not to mask the original
error, which is re-raised by the caller regardless). `run_with_heartbeat`'s own
`except BaseException:` clause is what invokes it — covering `ClaimLostError`, outer
`asyncio.CancelledError` (worker task cancelled during shutdown), and any unexpected exception
out of `renew_claim` itself. This is a single funnel, not three separate cleanup paths, which is
what makes "no orphan task survives worker exit" true by construction rather than by convention.

### Redis routing through `AsyncFrontier`

`run_with_heartbeat` takes an `AsyncFrontier`-wrapped frontier (every backend already holds
`self.frontier = AsyncFrontier(frontier)` from Step 4) and calls `await
frontier.renew_claim(claim)`. No new execution-boundary code was needed: `AsyncFrontier` already
offloads any non-`URLFrontier` backend's calls via `asyncio.to_thread` (Step 4), so Redis
renewals automatically never run on the event-loop thread — verified directly by
`tests/claim_heartbeat_test.py::TestRedisFrontierHeartbeat::test_redis_renewal_calls_do_not_run_on_the_event_loop_thread`.

### Overlap / cross-claim isolation

- **No overlapping renewals for the same claim** (requirement 14): `run_with_heartbeat`'s loop
  is strictly sequential — the next `asyncio.wait` iteration cannot begin until the previous
  `await frontier.renew_claim(claim)` has returned, so a slow renewal round trip simply delays
  the next tick rather than racing a second one. Proven under a synthetic 150ms-per-call Redis
  renewal delay by
  `test_overlapping_renewals_for_same_claim_do_not_happen` (max observed concurrency: 1).
- **No cross-worker interference** (requirement 15): each `run_with_heartbeat` call closes over
  its own `claim` (and thus its own `token`); the frontier's CAS validation (already built in
  Steps 1–3) means a renewal for claim A's token can never affect claim B's record, even for
  claims on different URLs running concurrently. Proven by
  `test_multiple_workers_renewing_different_claims_do_not_interfere` (local and Redis variants).

## Configuration

`FrontierConfig.heartbeat_interval: Optional[float] = None` — the only new knob, per the task's
"add only the minimum configuration required" instruction. Left `None` by default in both the
Pydantic model and `config.yaml` (commented out), meaning "derive automatically from
`lease_ttl`." The relationship is: **default heartbeat interval = `lease_ttl / 3`**, and an
explicit override is always clamped to stay below `lease_ttl / 2`. This is intentionally a
*derived* default rather than a second independent value operators must reason about in
lockstep with `lease_ttl` — matching the task's "prefer a derived/default interval rather than
requiring users to manually choose two unrelated values" guidance. `lease_ttl` itself already
existed (Step 4); nothing about its meaning or default (90s) changed here.

## Tests run

```
tests/claim_heartbeat_test.py                    20 passed  (new)
tests/crawler_heartbeat_integration_test.py        6 passed  (new)
tests/hybrid_crawler_test.py                        7 passed  (1 new, 6 regression)
tests/frontier_test.py                              8 passed  (regression)
tests/redis_frontier_test.py                       16 passed  (regression)
tests/crawler_test.py                               4 passed  (regression)
tests/extra_crawlers_test.py                 3 passed, 2 skipped (browser tests, unrelated)
tests/scrapling_crawler_test.py                     2 passed  (regression)
tests/manager_test.py                              10 passed  (regression)
tests/frontier_executor_test.py                     7 passed  (regression)
tests/crawler_manager_recovery_test.py              5 passed  (regression)

Subtotal: 88 passed, 2 skipped, 0 failed
```

Re-ran this full subset 3x back to back (it contains every timing-sensitive test this step
touches or could plausibly affect) — stable each time, no flakes.

Broader regression check, unaffected by this change, re-run to confirm:

```
tests/url_database_test.py, tests/url_utils_test.py, tests/discovery_test.py,
tests/parser_test.py, tests/main_cli_test.py, tests/media_evidence_test.py,
tests/streaming_manifest_test.py, tests/search_engine_test.py
26 passed, 0 failed

tests/tor_test.py, tests/fingerprinter_queue_test.py (don't reference the frontier;
run anyway for completeness, unlike prior steps which skipped them)
4 passed, 0 failed
```

Full `tests/` directory, run twice: **118 passed, 2 skipped, 0 failed** both times.

### What each required failure scenario maps to

| Scenario | Test |
|---|---|
| normal heartbeat keeps a claim alive beyond the original lease TTL | `TestRedisFrontierHeartbeat::test_heartbeat_keeps_claim_alive_beyond_original_lease_ttl` (+ negative control `test_without_heartbeat_a_slow_fetch_would_be_reclaimed`) |
| successful completion stops the heartbeat | `crawler_heartbeat_integration_test.py::test_successful_completion_stops_heartbeat` |
| failed completion stops the heartbeat | `crawler_heartbeat_integration_test.py::test_failed_completion_stops_heartbeat` |
| skipped completion stops the heartbeat | `crawler_heartbeat_integration_test.py::test_skipped_completion_never_starts_heartbeat` |
| worker exception stops the heartbeat | `crawler_heartbeat_integration_test.py::test_worker_exception_stops_heartbeat` |
| worker cancellation stops the heartbeat | `crawler_heartbeat_integration_test.py::test_worker_cancellation_stops_heartbeat` (+ unit-level `TestLocalFrontierHeartbeat::test_outer_cancellation_stops_heartbeat_and_drains_work_task`) |
| stale claim cannot be renewed | `TestLocalFrontierHeartbeat::test_stale_claim_cannot_be_renewed_and_raises_claim_lost` |
| claim reclaimed by another worker causes the original heartbeat to stop/fail safely | `TestRedisFrontierHeartbeat::test_claim_reclaimed_by_another_worker_causes_heartbeat_to_fail_safely` (+ crawler-level `crawler_heartbeat_integration_test.py::test_claim_lost_mid_fetch_is_not_marked_completed`) |
| Redis heartbeat calls do not execute on the asyncio event-loop thread | `TestRedisFrontierHeartbeat::test_redis_renewal_calls_do_not_run_on_the_event_loop_thread` |
| no orphan heartbeat tasks remain after worker completion | `TestLocalFrontierHeartbeat::test_no_orphan_task_remains_after_normal_completion` / `..._after_claim_lost` |
| multiple workers renewing different claims do not interfere with each other | `test_multiple_workers_renewing_different_claims_do_not_interfere` (local + Redis) |

Also covered, beyond the required list: the full `HybridCrawler` engine-escalation chain is
wrapped by a single heartbeat span
(`hybrid_crawler_test.py::test_hybrid_crawler_heartbeat_spans_full_engine_escalation_chain`),
and interval-derivation edge cases (`TestIntervalDerivation`, 6 tests: default/floor/fallback/
clamping behavior).

## Known limitations

- **`_run_engine_plan`'s per-engine attempts are not individually heartbeat-checkpointed.**
  The heartbeat renews on a fixed cadence regardless of which engine is currently running or how
  many engines have been tried; this matches the ADR's model (one claim, one heartbeat span,
  however many engines it takes) and was an explicit design point in Step 2's notes about
  escalation needing to reuse the same claim/token — not a gap introduced here.
- **A `renew_claim` call that itself raises (rather than returning `None`)** — e.g. a genuine
  Redis connection error mid-renewal, not a stale-claim rejection — propagates out of
  `run_with_heartbeat` as that raw exception, caught by each worker's generic `except Exception:`
  (which calls `mark_failed`). This seems like the right behavior (a transient infrastructure
  error shouldn't be silently treated as "claim lost" and abandoned — it should surface as a
  failure so `mark_failed`'s retry/backoff logic applies), but it means a flaky Redis connection
  during heartbeating produces a `mark_failed` rather than a clean retry-via-reclaim; not
  exercised by a dedicated test since `RedisURLFrontier.renew_claim` doesn't have an injectable
  failure mode without patching internals.
- **`heartbeat_interval` is fixed for a claim's entire lifetime**, computed once from the
  frontier's `lease_ttl` at crawler-backend construction time, not recomputed per-claim from
  `claim.lease_expires_at`. This matches the ADR §8 pseudocode's signature
  (`run_with_heartbeat(frontier, claim, coro, heartbeat_interval)` takes a static interval) and
  avoids a second timing source of truth; the tradeoff is that if `lease_ttl` were changed at
  runtime on a live frontier object (not a supported operation today — it's set at construction
  and only mutated directly in tests), already-constructed crawler backends would keep using
  their original interval.
- **No new Redis keyspace or Lua script changes.** `renew_claim`'s Lua script (Step 3) already
  satisfied every correctness property this step needed (token validation, atomic lease bump);
  requirement 17 explicitly called for not touching it absent a concrete correctness issue, and
  none was found.

Not proceeding to Step 6, per the task's instructions.
