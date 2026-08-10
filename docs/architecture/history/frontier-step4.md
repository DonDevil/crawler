# Frontier Migration — Step 4 Implementation Notes

Status: **implemented**. Scope for this step was set by the user after reviewing a focused
verification (see "Motivating verification" below), superseding the Redis-client-migration
framing originally sketched in `docs/architecture/frontier-adr.md` §8/§13.4-5. This step:

1. Introduces a non-blocking execution boundary (`asyncio.to_thread`) between asyncio crawler
   code and the synchronous `Frontier` protocol, so blocking Redis I/O never runs on the
   event-loop thread, without converting the `Frontier` protocol to `async` and without migrating
   to `redis.asyncio.Redis`.
2. Adds the asyncio background recovery task (ADR §7) to `core/crawler_manager.py`, wired to
   `RedisURLFrontier.reclaim_and_promote` (implemented in Step 3, not previously called by
   anything).
3. Wires the Redis/local frontier configuration knobs into `FrontierConfig` and `config.yaml`.

Heartbeat/claim-renewal wiring (ADR §8) remains Step 5, not started here.

## Motivating verification

Before this step, a focused verification (not part of this document's diff) traced the call path
`main.py → CrawlerManager → scheduler() → RedisURLFrontier.get_next_url() → redis-py client` and
confirmed: `core/redis_frontier.py` uses the synchronous `redis.Redis` client, and every one of
`get_next_url`/`mark_visited`/`mark_failed`/`mark_skipped`/`renew_claim`/`reclaim_and_promote` was
called directly (unawaited, unoffloaded) from `async def` methods in the six crawler backends'
`scheduler()`/`worker()` loops — meaning each call blocked the *entire* asyncio event loop for its
network round-trip duration, stalling every concurrently running fetch on that loop. Step 3's
single-round-trip Lua scripts bounded *how many* blocking calls happen per operation (fixing the
old O(domains) `SCAN` problem) but did not change *that* each one blocks the loop thread. This
step closes that gap.

## Files changed

- **`core/frontier_executor.py`** (new) — `AsyncFrontier`, the execution-boundary adapter
  described below.
- **`crawler/{async,http,tor,playwright,selenium,scrapling,hybrid}_crawler.py`** — each backend's
  `__init__` now does `self.frontier = AsyncFrontier(frontier)` instead of `self.frontier =
  frontier`; every existing frontier call site (`get_next_url`, `mark_skipped`, `mark_failed`,
  `mark_visited`, `has_pending`, `pending_count`, `add_url`, `get_source_query`) gained `await`.
  No other logic changed. `hybrid_crawler.py`'s sub-engine construction (`common_args`) passes the
  *raw* `frontier` parameter, not `self.frontier`, so each sub-engine (`AsyncCrawler`,
  `HTTPCrawler`, etc.) wraps the same underlying frontier independently instead of double-wrapping
  an already-wrapped adapter.
- **`core/scheduler.py`** — same mechanical edit (this class is still dead code, per Step 2's
  notes, but kept consistent).
- **`core/crawler_manager.py`** — new `self.async_frontier = AsyncFrontier(self.frontier)`
  attribute; new `_recovery_loop()` method; `run()` now starts/cancels a recovery task
  (`self._recovery_task`) around `await self._crawler.run()`; both `URLFrontier(...)` and
  `RedisURLFrontier(...)` construction now pass `max_retries`/`base_backoff`/`max_backoff`/
  `lease_ttl` (and, Redis-only, `domain_scan_limit`/`reclaim_batch_size`) from `FrontierConfig`
  instead of relying on each class's own defaults. `self.frontier` itself is untouched (still the
  raw synchronous object) — `prepare_frontier()` and its `add_url` calls are unchanged, since that
  runs synchronously at startup before any concurrent event-loop work exists (see "Deliberately
  out of scope" below).
- **`core/config.py`** — `FrontierConfig` gains `max_retries`, `base_backoff`, `max_backoff`,
  `lease_ttl`, `recovery_enabled`, `recovery_interval`, `reclaim_batch_size`, `domain_scan_limit`.
- **`config.yaml`** — documents the new knobs under `crawler.frontier`, with the same defaults as
  `FrontierConfig` (so this is additive/non-breaking for any existing config file missing them).
- **`tests/frontier_executor_test.py`** (new) — proves the execution boundary (see "Tests run").
- **`tests/crawler_manager_recovery_test.py`** (new) — proves the recovery task (see "Tests run").

Not touched: `core/frontier.py` (the `Frontier` protocol is still fully synchronous — requirement
6), `core/url_frontier.py`, `core/redis_frontier.py` (Step 3's keyspace/Lua scripts are unchanged
— no correctness problem was found that would justify touching them), any crawler backend's
fetch/parsing logic, the fingerprinter (not part of the frontier subsystem at all).

## Execution boundary: how blocking Redis calls are isolated from asyncio

`core/frontier_executor.py`'s `AsyncFrontier` wraps any `Frontier`-conforming object and exposes
`async` methods matching every operation the crawler loop needs. Internally:

```python
self._offload = not isinstance(frontier, URLFrontier)

async def _run(self, func, *args):
    if not self._offload:
        return func(*args)
    return await asyncio.to_thread(func, *args)
```

- **Local frontier (`URLFrontier`)**: calls run inline, synchronously, on the calling coroutine —
  zero added overhead, since there's no I/O to hide from the event loop (requirement 8).
- **Anything else (`RedisURLFrontier` today, any future networked backend automatically)**: calls
  are offloaded via `asyncio.to_thread`, which schedules the blocking call on the loop's shared
  default `ThreadPoolExecutor` and awaits the result — the event loop stays free to run other
  coroutines (other workers' `aiohttp`/`httpx` I/O) while the Redis round-trip is in flight.
- **Idempotent**: wrapping an already-wrapped `AsyncFrontier` reuses the same underlying object
  and offload decision instead of nesting adapters (nesting would hand a coroutine function to
  `asyncio.to_thread`, which is wrong). This is what lets `HybridCrawler` safely construct its six
  sub-engines from the same raw `frontier` without extra bookkeeping.
- **Bounded threads (requirement 9)**: `asyncio.to_thread` reuses `loop.run_in_executor` with the
  loop's default `ThreadPoolExecutor`, which caps at `min(32, os.cpu_count() + 4)` by default. This
  module does not create its own executor or spawn a thread per call —
  `test_concurrent_redis_calls_use_a_bounded_shared_thread_pool` fires 100 concurrent claims and
  asserts the set of distinct OS threads that actually touched Redis stays well under that cap.
- **Not applied to `Frontier` itself (requirement 6)**: `core/frontier.py`'s `Frontier` protocol
  is untouched — still plain `def`, not `async def`. `AsyncFrontier` is a separate, optional
  adapter that callers opt into.
- **Not a `redis.asyncio.Redis` migration (requirement 7)**: `core/redis_frontier.py` still uses
  the synchronous `redis-py` client exactly as Step 3 left it.

## Recovery behavior

`CrawlerManager._recovery_loop()`:

```python
async def _recovery_loop(self):
    interval = self.config.crawler.frontier.recovery_interval
    batch_size = self.config.crawler.frontier.reclaim_batch_size
    while True:
        try:
            reclaimed, requeued = await self.async_frontier.reclaim_and_promote(batch_size)
            ...
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Recovery sweep failed: {e}")
        await asyncio.sleep(interval)
```

- Started in `CrawlerManager.run()` alongside `self._crawler.run()`, **gated** on
  `frontier_config.recovery_enabled and hasattr(self.frontier, "reclaim_and_promote")` — the local
  frontier doesn't implement `reclaim_and_promote` (it already promotes due retries lazily inside
  its own `get_next_url()`, per ADR §10, so there's nothing for a recovery task to do there), so no
  task is created for it. This is a real gate on the actual frontier instance, not a config-only
  check — if Redis is configured but unreachable at startup, `CrawlerManager` already falls back to
  `URLFrontier` (pre-existing behavior), and the `hasattr` check correctly reflects that no
  recovery task is needed.
- Every tick calls `await self.async_frontier.reclaim_and_promote(batch_size)` — routed through the
  same non-blocking adapter, so the recovery sweep's Redis calls are also off the event-loop thread
  (requirement 16).
- Because Step 3's `reclaim_and_promote` already does both halves in one call — (a) reclaim
  abandoned inflight claims past their lease and requeue-or-terminalize them by the same
  attempt-vs-`max_retries` rule as `mark_failed`, (b) promote `retry_scheduled` entries whose
  backoff has elapsed back into their domain queue — wiring this loop is what makes **retry
  promotion automatic** for the Redis frontier (requirement 15): before this step, a retried URL
  sat in `retry_scheduled` until something called `reclaim_and_promote` manually (which nothing
  did outside tests). `test_retry_scheduled_urls_are_promoted_automatically_without_manual_calls`
  proves this end-to-end: `mark_failed` puts a URL in `retry_scheduled`, then — with no test code
  calling `reclaim_and_promote` — the running recovery task alone promotes it back to `queued` and
  it becomes claimable again with `attempt` incremented.
- Graceful shutdown: `run()`'s `finally` block does `self._recovery_task.cancel()` then
  `await asyncio.gather(self._recovery_task, return_exceptions=True)`, mirroring how each crawler
  backend already cancels its own `scheduler_task`/workers. The loop's own `except
  asyncio.CancelledError: raise` ensures cancellation isn't swallowed.
  `test_recovery_task_shuts_down_cleanly_with_crawler` asserts the task is `.done()` and
  `.cancelled()` (not left running, not raising) after `run()` returns.
- Errors inside a single sweep (`except Exception`) are logged and the loop continues rather than
  dying — a transient Redis hiccup shouldn't permanently stop recovery.

## Configuration added

`FrontierConfig` (`core/config.py`), all backwards-compatible additions (existing `config.yaml`
files without these keys keep working via defaults):

```python
max_retries: int = 3
base_backoff: float = 5.0
max_backoff: float = 300.0
lease_ttl: float = 90.0
recovery_enabled: bool = True
recovery_interval: float = 30.0
reclaim_batch_size: int = 200
domain_scan_limit: int = 50
```

`max_retries`/`base_backoff`/`max_backoff`/`lease_ttl` are passed into **both** `URLFrontier` and
`RedisURLFrontier` construction in `CrawlerManager.__init__` (previously only `RedisURLFrontier`
silently used its own hardcoded defaults, identical in value but not sourced from config) — this
is the "one shared `frontier.max_retries` config value, applied uniformly by whichever frontier
backend is active" ADR §4 calls for. `domain_scan_limit`/`reclaim_batch_size` are Redis-specific
and only passed to `RedisURLFrontier`. `recovery_enabled`/`recovery_interval`/`reclaim_batch_size`
are read directly by `CrawlerManager._recovery_loop`/`run`.

`config.yaml` mirrors these under `crawler.frontier` with the same default values, documented
inline.

## Tests run/results

```
tests/frontier_executor_test.py          7 passed   (new)
tests/crawler_manager_recovery_test.py   5 passed   (new)
tests/frontier_test.py                   8 passed   (local frontier, regression check)
tests/redis_frontier_test.py            16 passed   (Step 3 suite, regression check)
tests/crawler_test.py                    4 passed   (regression check)
tests/hybrid_crawler_test.py             6 passed   (regression check)
tests/extra_crawlers_test.py             3 passed, 2 skipped (browser tests, gated by
                                          RUN_BROWSER_CRAWLER_TESTS=1, unrelated)
tests/scrapling_crawler_test.py          2 passed   (regression check)
tests/manager_test.py                   10 passed   (regression check)

Total: 61 passed, 2 skipped, 0 failed
```

Re-ran 3x to check for flakiness in the timing-sensitive recovery/thread tests — stable each time.

Broader unrelated-suite regression check (unaffected by this change, re-run to confirm):
```
tests/url_database_test.py, tests/url_utils_test.py, tests/discovery_test.py,
tests/parser_test.py, tests/main_cli_test.py, tests/media_evidence_test.py,
tests/streaming_manifest_test.py, tests/search_engine_test.py
26 passed, 0 failed
```

`tests/tor_test.py` and `tests/fingerprinter_queue_test.py` don't reference the frontier and were
not run, consistent with Steps 1-3.

### New tests, what they prove

- `tests/frontier_executor_test.py`:
  - `TestLocalFrontierRunsInline` — `AsyncFrontier._offload is False` for `URLFrontier`, and every
    method call happens on the calling thread (`threading.get_ident()` compared before/after).
  - `TestRedisFrontierIsOffloaded` — `_offload is True` for `RedisURLFrontier`;
    `test_every_redis_operation_runs_off_the_event_loop_thread` spies on all 11 underlying
    frontier methods and asserts every single one executes on a different OS thread than the
    event loop; `test_concurrent_redis_calls_use_a_bounded_shared_thread_pool` fires 100 concurrent
    claims and asserts the distinct-thread count stays bounded (≤32) and all 100 claims are unique
    (proves both non-blocking behavior and continued correctness under concurrency post-wrapping).
  - `TestAsyncFrontierIdempotency` — wrapping twice reuses the same underlying frontier/offload
    decision.
- `tests/crawler_manager_recovery_test.py`:
  - `TestRecoveryTaskGating` — no task for the local frontier; no task when
    `recovery_enabled=False` even with Redis.
  - `TestRecoveryTaskBehavior` — periodic `reclaim_and_promote` calls (counted over ~4-5 ticks);
    automatic retry-scheduled promotion with no manual `reclaim_and_promote` call from the test;
    clean cancellation on shutdown.

## Deliberately out of scope / known limitations

- **`CrawlerManager.prepare_frontier()` and its `add_url` calls stay synchronous, unwrapped.**
  This runs once at startup (`run()` calls `self.prepare_frontier()` before starting the crawler
  or the recovery task), before any other coroutine is scheduled on the loop — blocking there
  doesn't stall concurrent work because there isn't any yet. It's also called synchronously by
  several existing `tests/manager_test.py` tests with no running event loop
  (`manager.prepare_frontier()` in a plain `def test_...` function) — making it `async` would
  silently break those without a large, unrelated test-suite rewrite, which requirement 11 ("do
  not modify unrelated crawler behavior") argues against. If a config populates thousands of seed
  URLs against a remote (non-localhost) Redis, this phase will take longer than it strictly needs
  to since it doesn't overlap with anything else — a real inefficiency, but not a stall of
  concurrent work, and not one of the six methods this task listed. Flagged here rather than
  silently left undocumented.
- **`CrawlerManager.frontier.close()` in `run()`'s `finally` is still a direct synchronous call.**
  It happens after the crawler and recovery task have both already stopped, with nothing else
  running on the loop at that point — same reasoning as above.
- **Heartbeat/claim-renewal (ADR §8) is still not wired.** `renew_claim` is implemented (Step 3)
  and covered by the offload tests here, but no crawler backend calls it — a legitimately slow
  fetch can still have its claim reclaimed as if the worker had crashed. Explicitly Step 5.
- **`asyncio.to_thread`, not `redis.asyncio.Redis`.** This was the user's explicit direction for
  this step (avoid the larger protocol-async migration for now). It fully solves "does this block
  the event loop" (offloaded work doesn't), but each offloaded call still costs a thread-pool
  hop (schedule + context switch) on top of the network round trip, and cannot batch multiple
  frontier operations into one Redis pipeline the way a native async client with connection reuse
  could. Under heavy Redis contention or very high worker concurrency, this could become a
  secondary bottleneck worth revisiting — not observed as a problem in testing, worth watching
  operationally.
- **Recovery interval vs. lease TTL tuning.** `recovery_interval` (default 30s) and `lease_ttl`
  (default 90s) are both now configurable but nothing enforces `recovery_interval < lease_ttl`; a
  misconfigured pair (e.g. `recovery_interval` larger than `lease_ttl`) would just mean recovery
  sweeps happen less often relative to how fast claims expire, not a correctness bug, but worth
  documenting as an operational footgun for whoever tunes `config.yaml` later.

Not proceeding to Step 5 (heartbeat/claim-renewal wiring), per the task's instructions.
