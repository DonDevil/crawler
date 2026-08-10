# Frontier Redis Failure Semantics (Step 7)

Roadmap position: `Redis outage semantics → failure visibility → domain
starvation → SQLite batching → final real-crawler validation → fingerprinter`.
This document covers only the first item. It is a correctness/reliability
fix, not a performance change — see
[`frontier-optimization-audit.md`](frontier-optimization-audit.md) §4.1–§4.3
and §7 for the audit that originally identified this gap; everything below
confirms, then closes, exactly what that audit found. §11 covers a follow-up
review finding (startup seeding could still silently lose a URL) folded into
this same document rather than a new one, since it's the same failure
contract applied to a code path the first pass under-covered.

## 1. Current (pre-fix) failure behavior

Every `RedisURLFrontier` method that talks to Redis wrapped its call in
`try/except redis.RedisError`, logged the error, and returned a value
**indistinguishable from a legitimate result**:

| Method | On Redis error, returned |
|---|---|
| `has_pending()` | `False` |
| `get_status_counts()` | all-zero dict |
| `pending_count()` | `0` (derived from the above) |
| `get_next_url()` | `None` |
| `add_url()` | `False` |
| `renew_claim()` | `None` |
| `mark_visited`/`mark_failed`/`mark_skipped` (`_complete`) | logged, returned `"error"` — a string never checked by any caller (the `Frontier` protocol declares these `-> None`) |
| `reclaim_and_promote()` | `(0, 0)` |

`AsyncCrawler.scheduler()` — and, identically, all 6 other crawler backends
(`http_crawler.py`, `hybrid_crawler.py`, `playwright_crawler.py`,
`selenium_crawler.py`, `scrapling_crawler.py`, `tor_crawler.py`; no shared
base class, each is an independent copy of the same loop) — used exactly
this signal to decide the crawl was finished:

```python
if self.queue.empty() and self._active_workers == 0 and not await self.frontier.has_pending():
    idle_loops += 1
    if idle_loops >= 10:  # ~5 seconds of idle
        self._stop_event.set()
```

## 2. Exact failure path traced

```
Redis becomes unreachable (network blip, restart, timeout)
    ↓
in-process queue happens to be empty, no worker holds a claim
    (an ordinary, frequent state — not rare)
    ↓
scheduler() calls get_next_url() -> RedisError swallowed -> returns None
    ↓
scheduler() calls has_pending() -> RedisError swallowed -> returns False
    ↓
"not has_pending()" reads as True -> idle_loops increments
    ↓
after ~5s of continuous polling under outage, idle_loops >= 10
    ↓
scheduler sets self._stop_event
    ↓
run() cancels every worker and the scheduler, crawler exits
    ↓
logged as "No more URLs to crawl" -- indistinguishable from a genuinely
completed crawl, while Redis in fact still holds the entire frontier
```

This is a full crawler shutdown triggered by an infrastructure blip, not a
completed crawl. Nothing outside a buried `logger.error` call inside
`has_pending`/`get_status_counts` signals that this happened.

Two secondary paths were traced and found to matter for the same reason:

- **`renew_claim` during a heartbeat.** `run_with_heartbeat`
  (`core/claim_heartbeat.py`) does `if renewed is None: raise
  ClaimLostError(claim)`. Since `renew_claim` returned `None` both for "the
  claim is genuinely stale" and "Redis errored," a transient Redis blip
  during a heartbeat renewal was indistinguishable from the claim having
  been legitimately reclaimed by another worker — the live worker aborted a
  possibly-still-succeeding fetch and consumed a real retry attempt for no
  reason.
- **`mark_visited`/`mark_failed`/`mark_skipped`.** `_complete()`'s `"error"`
  return value was structurally unreachable (the `Frontier` protocol
  declares these methods `-> None`, and no crawler backend checked the
  return anyway). A worker that successfully fetched a page but hit a Redis
  blip on `mark_visited` logged `"Processed (...): url [visited]"` and
  incremented its own counters while Redis's state still showed the URL
  `inflight` — silent duplicate-work risk (caught by lease-expiry recovery
  eventually, but wastefully).

`add_url`, `get_next_url` returning `None` in isolation, and
`reclaim_and_promote` were all confirmed safe in isolation (a dropped link,
a missed claim attempt, a no-op sweep are each individually recoverable) —
but they still violated the same principle (an infrastructure failure
silently became an ordinary return value), so the fix below applies
uniformly rather than fixing only the shutdown path and leaving the rest
inconsistent.

## 3. Corrected failure contract

Added `FrontierUnavailable` (`core/frontier.py`) — a single exception, not a
hierarchy (the smallest design that separates the two states the task cares
about):

```python
class FrontierUnavailable(Exception):
    """A frontier operation could not be completed because of an
    infrastructure failure in the backend (e.g. a Redis connection or
    timeout error), not because of the frontier's actual URL state."""
```

`RedisURLFrontier` now raises it from every method whose return value would
otherwise be ambiguous with a real result, instead of swallowing
`redis.RedisError` into a sentinel. `URLFrontier` (the local/SQLite backend)
has no infrastructure to fail against and never raises it — this is purely a
Redis-backend concern, and the local frontier is behaviorally unchanged.

### Semantics table

| Operation | Redis healthy | Redis temporarily unavailable | Redis permanently unavailable |
|---|---|---|---|
| `has_pending` | `True`/`False`, reflecting real cardinalities | raises `FrontierUnavailable` | raises `FrontierUnavailable` on every call — caller must not stop retrying+polling, but see §9 limitation |
| `get_status_counts` / `pending_count` | real counts | raises `FrontierUnavailable` | raises `FrontierUnavailable` on every call |
| `add_url` (crawl-time, from a worker) | `True` (queued) / `False` (dup/blacklisted) | raises `FrontierUnavailable` — URL is *not* silently dropped, worker abandons that link (§5) | raises on every call |
| `add_url` (startup seeding) | same | bounded retry (§11) absorbs it transparently | after bounded retry + circuit breaker, URL is durably deferred to `url_database`, not dropped (§11) |
| `get_next_url` | `FrontierClaim` or `None` (genuinely nothing eligible right now) | raises `FrontierUnavailable` — never returned as `None` | raises on every call |
| `mark_visited`/`mark_failed`/`mark_skipped` | applies, returns `None` | raises `FrontierUnavailable` — claim is *not* silently treated as resolved | raises on every call; claim stays `inflight` until lease expiry reclaims it once Redis returns |
| `renew_claim` | fresh claim, or `None` **only** for a genuinely stale/reclaimed token | raises `FrontierUnavailable` — no longer conflated with a stale claim | raises; `run_with_heartbeat` propagates it rather than misreporting `ClaimLostError` |
| `reclaim_and_promote` | `(reclaimed, requeued)` | raises `FrontierUnavailable` (caught by `crawler_manager._recovery_loop`'s existing broad `except Exception`, sweep just retries next interval) | same — sweep no-ops every interval until Redis returns, now visibly logged as a raised error rather than a silent `(0, 0)` |
| `clear` / `close` | applies | logs and swallows (unchanged — see §10, out of scope) | same |

The one governing rule, applied uniformly: **`FrontierUnavailable` must never
be read as "no work," "already handled," or "zero pending."** Every caller
that previously branched on a sentinel now either propagates the exception
or explicitly catches it and treats it as "unknown state, do not conclude
anything, retry later." §11 covers `add_url`'s startup-seeding row in full —
the crawl-time worker path (a discovered link failing to queue) and the
startup-seeding path (a seed/resume/discovered URL failing to queue) have
different callers with different constraints, so they resolved to different
policies without contradicting this rule.

## 4. Changes made

**`core/frontier.py`** — added `FrontierUnavailable`; documented the error
contract in the `Frontier` protocol's docstring (backends must raise it
rather than return an ambiguous sentinel).

**`core/redis_frontier.py`** — every `except redis.RedisError` branch in
`add_url`, `get_next_url`, `renew_claim`, `_complete` (shared by
`mark_visited`/`mark_failed`/`mark_skipped`), `reclaim_and_promote`,
`has_pending`, and `get_status_counts` now raises `FrontierUnavailable`
instead of returning a sentinel. `pending_count` needed no change — it
already delegates to `get_status_counts` with no `try` of its own, so the
exception propagates through it automatically. `get_source_query`, `clear`,
and `close` were deliberately left unchanged (§10).

**`core/frontier_executor.py`** — **no changes.** `AsyncFrontier` offloads
Redis calls via `asyncio.to_thread`, which already propagates an exception
raised in the offloaded call as the awaited coroutine's exception. The
adapter needed nothing new to carry `FrontierUnavailable` through.

**`core/claim_heartbeat.py`** — **no changes.** `run_with_heartbeat`'s `if
renewed is None: raise ClaimLostError(claim)` only fires for a genuine `None`
now (real Redis errors no longer produce one) — the misattribution described
in §2 is fixed as a direct consequence of the `redis_frontier.py` change,
with nothing to touch in this file.

**All 7 crawler backends** (`crawler/async_crawler.py`,
`crawler/http_crawler.py`, `crawler/hybrid_crawler.py`,
`crawler/playwright_crawler.py`, `crawler/selenium_crawler.py`,
`crawler/scrapling_crawler.py`, `crawler/tor_crawler.py`) — identical,
targeted patch applied to each (there is no shared base class to change
once; each backend duplicates this loop independently, and fixing the bug
required touching all 7, not refactoring them):

- `scheduler()`: `get_next_url()` and the `has_pending()` check are each
  wrapped in `try/except FrontierUnavailable`. On that exception, `idle_loops`
  is reset to `0` (never incremented) and the loop falls through to its
  existing `asyncio.sleep(0.5)` before retrying — so an outage is retried on
  the same cadence as normal polling, never converted into idle/shutdown
  evidence, but also never causing a busy-loop.
- `worker()`: a new `except FrontierUnavailable` clause (parallel to the
  existing `except ClaimLostError`) logs and abandons the claim *without*
  attempting a completion call — lease-based recovery (`reclaim_and_promote`)
  picks it up once Redis is reachable again. The two pre-existing
  `mark_failed(...)` calls (on `asyncio.CancelledError` and in the generic
  `except Exception` fallback) are now wrapped in their own narrow
  `try/except FrontierUnavailable`, because introducing this exception into
  `mark_failed` meant those two call sites could now raise where they
  previously never could — un-guarded, a `FrontierUnavailable` there would
  either mask `CancelledError`'s propagation or leak out of `worker()`
  uncaught, permanently killing that worker task (a strict regression this
  fix must not introduce).

**`core/crawler_manager.py`** — see §11. (First pass introduced a simpler
catch-and-skip helper here; the follow-up review in §11 replaced it with the
bounded-retry + circuit-breaker + durable-defer policy described there,
because catch-and-skip alone reintroduced exactly the kind of silent URL
loss this whole document exists to close.)

## 5. Retry / recovery semantics

High-level desired behavior (as specified): outage → crawler does not
conclude "finished" → error surfaced → controlled retry/backoff → Redis
recovers → crawler continues. What was and wasn't built:

- **No new automatic retry-the-Redis-call mechanism was added at the
  crawl-time (worker/scheduler) layer.** The scheduler's existing poll loop
  (`get_next_url`/`has_pending` every 0.5s) already *is* a natural retry
  with backoff-shaped cadence once `FrontierUnavailable` stops being read as
  terminal — adding a second, independent retry layer on top would be
  complexity with no behavioral benefit, and risks exactly the kind of
  "retry an ambiguous mutation" hazard the task warned against. (Startup
  seeding is a separate, bounded exception to this — see §11 — because it
  runs once per process start rather than continuously, so a small bounded
  retry there does not create a persistent retry layer.)
- **Mutating operations are never auto-retried at the crawl-time layer.**
  `add_url` and `mark_*`, called from a worker, raise and stop — the worker
  does not immediately retry the same call. This matters for the
  ambiguous-timeout case: a Redis client timeout does not tell you whether
  the command reached the server. For `add_url` (whose Lua script does
  dedup-then-insert atomically), retrying after a timeout does not need to
  resolve that ambiguity: the retry either finds the URL already `known`
  (server-side success, client-side timeout) and correctly reports `False`,
  or finds it genuinely absent (server-side failure) and correctly reports
  `True` — either way the retry's result is correct and safe, so a
  *caller-driven* retry (as opposed to a hidden one inside the frontier
  method) is not dangerous here. For `mark_*`, retrying is similarly safe
  *if* it reuses the same claim token — `_complete_claim_script` re-checks
  `current_token == token` and is naturally idempotent (a second call with a
  stale/already-consumed token returns `"stale"` and does nothing). Given
  that safety exists structurally, the crawl-time layer still does not add
  automatic retries for these — it leaves the decision at the worker level,
  where lease-based recovery already provides a correct (if slower)
  fallback path for exactly this case, matching the "smallest necessary
  change" directive. (§11's startup-seeding retry uses this same safety
  property deliberately — see there.)
- **Read-only status operations (`has_pending`, `get_status_counts`) are
  effectively retried by the poll loop itself** — no dedicated retry code
  needed since polling already recurs every 0.5s.
- **`renew_claim`** is not retried inside the heartbeat call — a raised
  `FrontierUnavailable` propagates out of `run_with_heartbeat` (via its
  existing `except BaseException` cleanup path) to the worker, which
  abandons the claim without marking completion. This intentionally
  sacrifices the in-flight fetch's result on a Redis outage during
  heartbeating, rather than risk the lease legitimately expiring underneath
  a renewal retry loop and creating a second concurrent claim owner.

## 6. Ambiguous-timeout considerations

Every Lua-script-backed mutation (`add_url`, `get_next_url`/`claim_next`,
`_complete`/mark_*, `renew_claim`, `reclaim_and_promote`) executes as one
atomic Redis-side operation — a client-side timeout means "I don't know if
this ran," never "it partially ran." Given the token-CAS design already
audited and confirmed sound
([`frontier-optimization-audit.md`](frontier-optimization-audit.md) §3), a
caller-driven retry after any of these is safe to attempt (idempotent by
construction: `add_url` re-checks `SISMEMBER`, completion re-checks the
claim token). This safety property is exactly what makes §11's bounded
startup-seeding retry safe to add without reopening a correctness question.

## 7. Tests added (Step 7 initial pass)

New file: `tests/frontier_redis_failure_semantics_test.py` (17 tests, Redis
DB 2 — reserved for this suite only, never DB 0/production, dedicated
namespace `test_failure_semantics` distinct from `tests/redis_frontier_test.py`'s
DB 1). Failures are injected deterministically by patching the specific Lua
`Script` object or `.pipeline()` call each method uses (per the task's
stated preference over repeatedly stopping a real Redis process):

| Test class | Covers |
|---|---|
| `TestHasPendingAndStatusCountsNeverFakeEmpty` | Test 1 & 2 — `has_pending`/`get_status_counts`/`pending_count` raise instead of reporting false/zero; healthy-path sanity check |
| `TestGetNextUrlNeverFakesIdle` | `get_next_url` raises instead of `None`; healthy-path sanity check |
| `TestAddUrlNeverSilentlyLosesUrls` | Test 5 — raises instead of `False`; confirms a failed add left no partial state (URL still addable after recovery) |
| `TestCompletionFailuresAreVisible` | Test 6 — parametrized over `mark_visited`/`mark_failed`/`mark_skipped`; confirms the claim stays `inflight` (not falsely resolved) after a failed completion, and that a real completion still works once Redis recovers |
| `TestRenewClaimDistinguishesOutageFromLostClaim` | Confirms `renew_claim` raises on a Redis error but still returns a clean `None` for a genuinely stale claim (the exact conflation described in §2) |
| `TestReclaimAndPromoteVisibility` | Raises instead of returning `(0, 0)` |
| `TestSchedulerNeverTreatsOutageAsIdle` | Test 3 & 4 — runs the real `AsyncCrawler.scheduler()` coroutine against a Redis frontier with pending work, injects failures on `get_next_url`/`has_pending`, and asserts `_stop_event` is never set for well past the 10-idle-loop (~5s) shutdown threshold; then lifts the injected failure and asserts the pending URL is claimed and the scheduler still hasn't stopped; a third test confirms a *genuinely* empty frontier (no injected failure) still shuts the scheduler down normally |

Test 7 (existing healthy behavior) — the full pre-existing frontier/crawler
suite was run against the changes.

## 8. Test results (Step 7 initial pass)

```
tests/frontier_redis_failure_semantics_test.py ......................  17 passed
tests/redis_frontier_test.py, frontier_executor_test.py, frontier_test.py,
crawler_manager_recovery_test.py, claim_heartbeat_test.py               72 passed (1 pre-existing flake deselected, see below)
Full repo suite (tests/, excluding tests/report.py)                     143 passed, 2 skipped, 1 deselected
```

One pre-existing flake was found and confirmed **unrelated** to this change:
`frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`
fails intermittently on unmodified `main` too (reproduced by `git stash`-ing
this work and re-running it twice, both times failing identically before any
of these changes existed). Not touched, not caused by this work.

## 9. Remaining known limitations (Step 7 initial pass)

- **Permanently-down Redis still polls forever at crawl time, by design.**
  There is no circuit breaker at the scheduler/worker layer — it will retry
  every 0.5s indefinitely. This matches the task's explicit instruction not
  to add retry-policy changes beyond what outage semantics require, and not
  to change worker counts/rate limiting/lease TTL. An operator-facing
  distinction between "Redis will come back" and "Redis is gone for good" is
  a policy decision out of scope here. (Startup seeding *does* get a circuit
  breaker — §11 — because it is a bounded, one-shot pass where unbounded
  per-URL retry cost is a real operational problem; the always-on crawl loop
  is a different situation where "keep polling forever" is the correct,
  already-tested behavior.)
- **Worker-cancellation-during-blocking-Redis-call thread orphaning**
  (`frontier-optimization-audit.md` §7's last row: `asyncio.to_thread`
  cannot forcibly kill an in-flight blocking `redis-py` call) is unchanged
  and out of scope — it is an `asyncio.to_thread`/offload-boundary concern,
  not a failure-semantics one.
- **No live Redis stop/start integration test was run.** The task allowed
  this ("if practical" / "if useful") in addition to mocked failure
  injection, but recommended mocked injection as the primary method and
  cautioned against repeatedly killing a shared Redis process. Since the
  local Redis instance available in this environment is not confirmed to be
  a disposable, exclusively-test-owned instance, and the mocked tests
  already exercise every scenario the task's Phase 8 lists (available →
  unavailable → returns → succeeds again) deterministically and repeatably,
  a live stop/start test was not performed.
- **`reclaim_and_promote` raising during the recovery loop was not given a
  dedicated new test beyond the direct unit test** (`TestReclaimAndPromoteVisibility`)
  — `crawler_manager_recovery_test.py`'s existing tests already exercise the
  loop's broad `except Exception` handling end-to-end for other error types,
  and that handling needed no code change, so no new integration test was
  added for this specific path.

## 10. What was explicitly NOT changed (applies to both passes)

- Lua scripts / Redis keyspace / Redis data model
- `redis.asyncio` migration
- worker counts, domain scheduling, rate limiting, heartbeat cadence, lease
  TTL, or retry-count policy
- `is_blacklisted()` / blacklist file or caching behavior
- SQLite frontier (`URLFrontier`) behavior — it has no Redis to fail against
  and is unchanged
- `clear()`/`close()` — left swallowing `RedisError` (testing/reset-only and
  shutdown-time operations respectively; not on any correctness-critical
  path, not reachable from production crawl-loop decisions)
- `get_source_query()` — left returning `""` on error; non-critical metadata
  (priority/logging only), not one of the traced callers in the task's
  Phase 1 list
- Benchmark scripts, throughput optimization work, or any further
  performance investigation
- Refactoring the 7 crawler backends into a shared base class (the
  duplication predates this change; fixing the bug required touching each
  copy, not consolidating them)
- The already-tested scheduler outage behavior (§4/§7) — the follow-up
  review in §11 touches only `core/crawler_manager.py`'s startup-seeding
  loaders, not `crawler/*.py` or `core/redis_frontier.py`

## 11. Startup seeding failure semantics (follow-up review)

### 11.1 The gap

The Step 7 initial pass made `RedisURLFrontier.add_url()` raise
`FrontierUnavailable` instead of returning `False` on a Redis error (§3),
but the first version of `CrawlerManager.load_seed_urls()` /
`load_unfinished_urls()` / `load_search_query_urls()` simply caught that
exception, logged it, and moved to the next URL:

```
seed URL -> add_url() -> Redis temporarily unavailable -> FrontierUnavailable
    -> caught, logged -> URL skipped -> seeding continues
```

Unless that URL happened to be re-discovered later (seed files and resume
lists are not re-scanned mid-run), it was gone for the rest of the crawl —
exactly the "`FrontierUnavailable` interpreted as URL handled" failure this
whole document exists to prevent, just relocated to the seeding path instead
of the shutdown path.

### 11.2 Why startup seeding is a different problem than the crawl loop

The crawl-time `add_url` call (a worker discovering a link mid-crawl, §5) and
the startup-seeding `add_url` call are not the same situation:

- A worker's `add_url` failure is one link among many the page produced,
  discovered again in effect never (the page won't be re-fetched just to
  re-discover it) — but the crawl loop itself keeps running, and dropping
  one link is a bounded, low-severity loss consistent with normal web-crawl
  behavior (links get missed for all sorts of reasons).
- A **seed** or **resume** URL is an explicit, operator-provided or
  previously-recorded starting point, generally provided in much smaller
  volume, and seeding runs exactly once at process start with no ongoing
  loop to naturally retry it later. Losing one silently is a much more
  concrete regression (a piece of the operator's actual input vanishing).

This is why §5 deliberately does *not* add crawl-time retries, while this
section deliberately does add a small amount of retry/deferral machinery
scoped to seeding only.

### 11.3 Options considered

1. **Retry the current insertion with bounded backoff.** Absorbs brief
   blips for free, but alone does not bound worst-case behavior under a
   sustained outage — retrying every URL in a large seed file would make
   startup latency scale with seed-list size.
2. **Retain failed URLs for retry after Redis recovery.** Guarantees no
   loss, but naively "retry until it works" risks becoming the "uncontrolled
   infinite startup loop" the task explicitly forbids.
3. **Abort/pause the whole seeding pass on the first `FrontierUnavailable`.**
   Simple and loud, but throws away every URL not yet attempted (a seed file
   with 5,000 URLs where the 3rd one hits a millisecond-scale blip would
   abandon the other 4,997 for no good reason) and gives the operator no
   partial progress.
4. **Reuse the architecture's own resume mechanism.** `url_database`
   (SQLite) already persists per-URL status, and `load_unfinished_urls`
   already exists specifically to re-add `queued`/`pending` URLs to the
   frontier on a `--unfinished` run. A URL the frontier couldn't accept can
   be written directly into that same table with status `"queued"` — no new
   table, no new CLI flag, no new subsystem — and it becomes automatically
   eligible for the *existing* resume path.

**Chosen: a combination of 1, 2 (via 4), and a bounded circuit breaker**,
not any single option in isolation — each alone left a requirement unmet
(1 alone doesn't bound worst-case latency; 2 alone without a breaker risks
unbounded retry; 3 alone throws away good URLs; 4 alone, with no retry
first, would defer even URLs that would have succeeded a moment later).
Concretely, implemented in `CrawlerManager._make_seed_url_adder()`
(`core/crawler_manager.py`), a small stateful closure shared by all three
loaders:

- **Bounded per-URL retry.** Up to `_SEED_ADD_MAX_ATTEMPTS = 3` attempts,
  `_SEED_ADD_RETRY_DELAY_SECONDS = 0.5` between them (plain `time.sleep` —
  `prepare_frontier()` runs synchronously before any crawl worker/task
  exists, per `core/frontier_executor.py`'s `AsyncFrontier` docstring, so
  this blocks nothing else). Absorbs a brief blip transparently — the URL
  ends up genuinely queued in Redis, not deferred.
- **Circuit breaker.** After `_SEED_ADD_CIRCUIT_BREAKER_THRESHOLD = 3`
  consecutive URLs each exhaust their retry budget, further URLs in *that
  loader call* skip retrying entirely and are deferred immediately. This is
  what bounds worst-case startup latency independent of seed-list size
  (requirement 3) — a real outage cannot cost more than
  `3 URLs × 3 attempts × 0.5s ≈ 4.5s` of retry time total, no matter how
  many thousands of URLs are being seeded. The counter resets to `0` the
  moment a URL succeeds, so a flapping-but-mostly-healthy Redis doesn't get
  stuck on the fast-defer path.
- **Durable defer, not drop.** Whether reached via exhausted retries or the
  breaker, a URL that couldn't be queued is written directly to
  `url_database` with status `"queued"` (bypassing the frontier). This
  reuses `load_unfinished_urls`'s existing query
  (`get_urls_and_statuses(["queued", "pending"])` unchanged) — no new
  status value, no new table. The URL becomes automatically recoverable on
  a subsequent `--unfinished` run once Redis is healthy again, with zero
  new code on the recovery side.
- **Distinguishable outcomes.** The adder returns one of `"accepted"`,
  `"rejected"` (ordinary duplicate/blacklist — ordinary `add_url() ->
  False`, no exception, ordinary path unaffected), or `"deferred"`. Each
  loader tracks its own `deferred` count separately from `accepted`, and
  logs a distinct `ERROR`-level summary line (via a shared
  `_log_deferred_urls` helper) naming the count and pointing at
  `--unfinished` as the recovery path, whenever `deferred > 0`.

### 11.4 CLI modes considered

- **`--seed-file`** → `load_seed_urls()`: covered directly.
- **`--query`** → `load_search_query_urls()`: covered directly; the
  discovered-URL loop now routes through the same adder.
- **`--query-only`** only changes `include_seed_files`; doesn't change which
  loader needs protection.
- **`--unfinished`** (the actual resume flag; there is no separate
  `--resume` or `--max-crawl` flag in `main.py` — `--max-pages` is the page
  cap) → `load_unfinished_urls()`: covered directly, and deliberately —
  since this loader is itself one of the three, an outage *during a resume
  attempt* must not re-lose the URL either, or a crawl could never recover
  from a Redis outage that happens to coincide with every resume attempt. A
  URL that fails during `load_unfinished_urls()` is written back to
  `url_database` with status `"queued"`, i.e. it simply remains eligible for
  the next `--unfinished` run — no special-casing needed, because "queued"
  is already the status this loader looks for.
- **`--indefinite-run`** / **`--max-pages`**: control `set_max_pages()`,
  unrelated to seeding; no interaction with this change.

### 11.5 Requirements check

1. *A URL must never be silently discarded because Redis was unavailable* —
   satisfied: every non-accepted, non-ordinary-rejected URL is durably
   persisted to `url_database`, never merely logged and dropped.
2. *Redis errors must remain distinguishable from duplicate/blacklisted
   URLs* — satisfied: `"deferred"` vs. `"rejected"` are distinct return
   values and distinct log lines; blacklisted URLs never even reach a Redis
   call (`URLUtils.clean_url`/`is_blacklisted` filter client-side first), so
   they cannot be conflated with a Redis failure.
3. *Permanently unavailable Redis must not create an uncontrolled infinite
   startup loop* — satisfied: both the per-URL attempt count and the
   consecutive-failure circuit breaker are finite constants; worst-case
   added latency per loader call is bounded and independent of seed-list
   size.
4. *Existing successful seeding behavior must remain unchanged* — satisfied:
   on the happy path, the adder makes exactly one `add_url` call per URL
   (verified by test, §11.6) with no sleep, no extra `url_database` write
   beyond what `RedisURLFrontier.add_url` already does internally.
5. *Do not introduce a new global retry framework unless genuinely
   necessary* — satisfied: the retry/breaker state lives in a local closure
   scoped to one loader call, not a new module, class hierarchy, or
   configuration surface.
6. *Keep the change localized* — satisfied: only `core/crawler_manager.py`
   was touched; `core/redis_frontier.py`, the Lua scripts, and all 7
   crawler backends' scheduler/worker code (§4, already reviewed and tested)
   are untouched.
7. *Preserve the Step 7 scheduler/recovery semantics already implemented* —
   satisfied by construction: this section changes nothing reachable from
   the crawl loop, only the one-time startup seeding path that runs before
   `self._crawler.run()` is ever called.

### 11.6 Tests added

New file: `tests/crawler_manager_seed_failure_semantics_test.py` (8 tests,
Redis DB 2, namespaces distinct from both the earlier `tests/redis_frontier_test.py`
(DB 1) and `tests/frontier_redis_failure_semantics_test.py` (DB 2,
`test_failure_semantics`) suites):

| Test | Proves |
|---|---|
| `test_transient_failure_is_absorbed_by_retry_within_the_same_run` | A blip that fails twice then succeeds (patched with a counting `side_effect`) ends with the URL genuinely claimable from Redis within the same run, using exactly the expected 3 script calls |
| `test_sustained_outage_defers_url_to_storage_and_resume_recovers_it` | Redis down for the entire `load_seed_urls()` call → URL never enters Redis but is recorded in `url_database` as `"queued"` → a fresh `CrawlerManager` (simulating the next process run) with `resume_unfinished=True` calls `load_unfinished_urls()` and the URL becomes genuinely claimable |
| `test_unfinished_loader_itself_also_defers_rather_than_loses_urls_on_outage` | An outage *during* `load_unfinished_urls()` itself (not just the seed-file loader) also defers rather than drops (§11.4's resume-of-resume case) |
| `test_duplicate_url_is_still_rejected_not_deferred` | The same URL seeded twice makes exactly 2 (not 3+) `add_url` calls, no retries triggered, `queued` count stays 1 |
| `test_blacklisted_url_is_still_rejected_without_touching_redis` | A blacklisted seed URL never calls `_add_url_script` at all (`spy.assert_not_called()`) — confirms blacklist filtering is unaffected and happens before any Redis interaction |
| `test_healthy_seeding_uses_exactly_one_call_per_url_no_wasted_retries` | 5 URLs, healthy Redis, spied via `wraps=` → exactly 5 calls, 5 accepted, no sleeps, no deferrals (requirement 4) |
| `test_circuit_breaker_stops_wasting_retries_but_still_defers_every_url` | 6 URLs, Redis down throughout → exactly `3 × 3 = 9` script calls (not `6 × 3 = 18`), proving the breaker trips after 3 consecutive exhausted URLs; all 6 still end up durably recorded (requirement 3 + 1 together) |
| `test_adder_returns_accepted_rejected_deferred_correctly` | Direct unit coverage of `_make_seed_url_adder()`'s three return values in sequence (also stands in for `load_search_query_urls`, whose per-item loop uses the identical adder but is harder to drive end-to-end without live search-engine discovery) |

All outage-scenario tests patch `core.crawler_manager._SEED_ADD_RETRY_DELAY_SECONDS`
down to `0.01s` so the suite stays fast — the behavior under test is the
retry/circuit-breaker *logic*, not real-world timing.

### 11.7 Test results

```
tests/crawler_manager_seed_failure_semantics_test.py  8 passed
Full repo suite (tests/, excluding tests/report.py)    151 passed, 2 skipped, 1 deselected
```

The 1 deselected test is the same pre-existing, unrelated flake noted in §8
(`test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`). No new
failures or regressions in any previously-passing test, including the full
Step 7 initial-pass suite (§7/§8) and `tests/crawler_manager_recovery_test.py`.

### 11.8 Remaining limitations

- **`load_search_query_urls` was not exercised end-to-end under injected
  failure** — it shares the identical `_make_seed_url_adder()` logic
  covered directly by unit tests (§11.6), but driving it through a real
  `discover_urls_from_queries_with_report()` call would require mocking
  live search-engine discovery, which is out of scope for this focused fix.
- **A deferred URL's priority is fixed at defer time.** `url_database.add_url`
  only stores `status`, not priority — when a deferred URL is later re-added
  via `load_unfinished_urls`, its priority is recomputed by
  `_priority_for_unfinished_url` (based on status, not on the original
  seed/discovery priority). This matches how `load_unfinished_urls` already
  treats every other resumed URL, so it is not a new inconsistency, but it
  does mean a high-priority discovered URL that got deferred loses that
  specific priority value on resume — an acceptable, pre-existing limitation
  of the resume mechanism this fix reuses, not one this fix introduces.
- **The circuit breaker is scoped per loader call, not global across a
  single `prepare_frontier()` invocation.** `load_seed_urls()` and
  `load_search_query_urls()` (both called for a normal, non-`--unfinished`
  run) each get their own fresh breaker. In the worst case this roughly
  doubles the bounded worst-case retry cost described in §11.3 (once per
  loader, not once per process) — still a small constant, not scaling with
  URL count, so this was accepted rather than adding cross-loader state for
  a marginal latency difference.
- **No operator-facing exit code or hard failure signals "seeding was
  incomplete."** Deferred URLs are logged at `ERROR` level with an explicit
  count and remediation instruction (`--unfinished`), consistent with how
  the rest of this codebase surfaces problems (log-based, no custom exit
  codes anywhere in `main.py`), but a fully automated pipeline watching only
  the process exit code would not detect this on its own.

---

```
ROOT CAUSE:
RedisURLFrontier caught redis.RedisError in has_pending()/get_status_counts()
(and, less severely, in add_url/get_next_url/mark_*/renew_claim/
reclaim_and_promote) and returned an ordinary sentinel (False/0/None/(0,0))
instead of signaling failure. All 7 crawler backends' scheduler() used
`not await self.frontier.has_pending()` as authoritative evidence the crawl
was done, so a transient Redis outage was silently indistinguishable from a
genuinely empty frontier and could trigger full crawler shutdown after
~5 seconds of polling under outage. A follow-up review then found the fix's
own startup-seeding helper (catch FrontierUnavailable, log, skip) silently
lost seed/resume/discovered URLs on the same class of outage -- the same
bug relocated to a different call site.

FAILURE CONTRACT:
Added FrontierUnavailable (core/frontier.py). RedisURLFrontier raises it
from every method whose sentinel return value would otherwise be ambiguous
with legitimate frontier state. Callers must treat it as "unknown state, do
not conclude anything, retry later" -- never as no-work/already-handled/
zero-pending. URLFrontier (local/SQLite) is unaffected -- it has no
infrastructure to fail against. For startup seeding specifically: bounded
retry (3 attempts) + a per-loader-call circuit breaker (3 consecutive
exhausted URLs) + durable defer into url_database's existing "queued"
status, recoverable via the existing --unfinished resume path -- never a
silent drop.

FILES CHANGED:
core/frontier.py, core/redis_frontier.py, core/crawler_manager.py,
crawler/async_crawler.py, crawler/http_crawler.py, crawler/hybrid_crawler.py,
crawler/playwright_crawler.py, crawler/selenium_crawler.py,
crawler/scrapling_crawler.py, crawler/tor_crawler.py
(core/frontier_executor.py and core/claim_heartbeat.py needed no changes --
the fix propagates through them automatically. The follow-up review touched
only core/crawler_manager.py again -- no other file changed a second time.)

TESTS ADDED:
tests/frontier_redis_failure_semantics_test.py -- 17 tests (Step 7 initial
pass) covering has_pending/get_status_counts/pending_count/get_next_url/
add_url/mark_visited/mark_failed/mark_skipped/renew_claim/reclaim_and_promote
raising FrontierUnavailable on Redis failure, plus 3 tests running the real
AsyncCrawler.scheduler() against injected outage/recovery/genuine-empty
scenarios.
tests/crawler_manager_seed_failure_semantics_test.py -- 8 tests (follow-up
review) covering transient-blip absorption, sustained-outage defer +
--unfinished recovery, the resume loader's own outage protection, ordinary
duplicate/blacklist behavior unaffected, the circuit breaker bounding retry
cost, and the healthy path making no extra calls.

TESTS PASSED:
25/25 new tests (17 + 8). Full repo suite: 151 passed, 2 skipped
(pre-existing, unrelated), 1 deselected (the same pre-existing flaky test
noted in §8, confirmed unrelated by reproducing it on unmodified main).

REMAINING RISKS:
Permanently-down Redis polls forever at the crawl-time (scheduler/worker)
layer by design -- no circuit breaker there, since that layer is meant to
run indefinitely and the existing poll cadence already bounds per-cycle
cost. Startup seeding does have a circuit breaker (bounded, ~4.5s worst
case per loader call, independent of seed-list size). Worker-cancellation-
during-blocking-Redis-call thread orphaning (pre-existing, documented in
frontier-optimization-audit.md §7) is unchanged. No live Redis stop/start
integration test was run, in favor of deterministic mocked failure
injection covering the same scenarios. A deferred URL's original priority
is not preserved across a --unfinished resume (matches existing
load_unfinished_urls behavior for every other resumed URL, not a new gap).
```
