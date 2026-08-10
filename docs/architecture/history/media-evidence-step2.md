# Media Evidence — Step 2: Async Execution Boundary

Status: **implemented**. Scope was set by the user after reviewing
[`fetch-extractor-audit.md`](../fetch-extractor-audit.md) (§8/§14), which
identified this as the highest-confidence production hot-path issue in the
fetch/extractor pipeline: synchronous Redis Media Evidence writes running
directly on the asyncio event loop. This step closes that gap, and only
that gap — no other audit finding (`prefers_browser()` wiring, Playwright's
doubled timeout, per-fetch timing, Tor-unavailable signaling, Yandex
cooldown granularity, dead-code removal) was touched.

Builds on [`media-evidence-step1.md`](../media-evidence-step1.md) (the
storage/coordination layer this step wraps) and mirrors the pattern
[`frontier-step4.md`](frontier-step4.md) established for the frontier's own
synchronous-Redis-client problem — see "Relationship to the frontier fix"
below.

## Motivating finding

`docs/architecture/fetch-extractor-audit.md` §8, confirmed by grep across
all six crawler engines: every `worker()` called
`self.media_database.record_media_link(...)` (and, for
`AsyncCrawler`/`HTTPCrawler`/`TorCrawler`'s direct-response media path,
`record_manifest_variants(...)`) with no `await`, no `asyncio.to_thread`.
`storage/redis_media_evidence_store.py`'s `record_media_link` is a plain
synchronous `def` using a synchronous `redis-py` client
(`self.redis_conn`) — every call performs real blocking network I/O on the
single OS thread the asyncio event loop runs on, freezing every other
concurrently-scheduled coroutine (every other in-flight fetch across every
engine, the scheduler task, the recovery task) for that round trip's
duration. Called once per discovered media link per page (0–many times)
plus once more per parsed streaming manifest — on a media-dense piracy
site, not a rare edge case, and live in the default `auto` (`HybridCrawler`)
path whenever Media Evidence is enabled (the default).

The frontier had exactly this defect once (`frontier-step4.md`) and it was
fixed with a non-blocking `asyncio.to_thread` adapter (`AsyncFrontier`,
`core/frontier_executor.py`). That fix never touched Media Evidence — this
step is the same fix, applied to the second, previously-unfixed instance of
the same class of bug.

## Files changed

- **`core/media_evidence_executor.py`** (new) — `AsyncMediaEvidence`, the
  execution-boundary adapter described below.
- **`crawler/{hybrid,async,http,tor,playwright,selenium,scrapling}_crawler.py`**
  — each backend's `__init__` now does
  `self.media_database = AsyncMediaEvidence(media_database) if media_database
  is not None else None` instead of `self.media_database = media_database`;
  every existing `record_media_link`/`record_manifest_variants` call site
  (15 across the seven files, including `playwright_crawler.py`'s
  `_route_request`/`_capture_response` network-interception callbacks, which
  are already `async def`) gained `await`. No other logic changed — the
  same `try/except Exception as exc: logger.debug(...)` blocks, and the
  same *absence* of a dedicated try/except in `async_crawler.py`/
  `http_crawler.py`/`tor_crawler.py`'s `fetch()` direct-response path (where
  a `record_media_link` failure already fell through to the surrounding
  fetch-retry `except Exception`, before this change and after it), are
  preserved exactly. `hybrid_crawler.py`'s sub-engine construction
  (`common_args`) passes the *raw* `media_database` parameter, not
  `self.media_database`, so each sub-engine wraps the same underlying store
  independently instead of double-wrapping an already-wrapped adapter —
  the identical pattern `frontier-step4.md` established for `frontier`.
- **`tests/media_evidence_executor_test.py`** (new) — proves the execution
  boundary (see "Tests run/results").
- **`tests/benchmarks/media_evidence_benchmark.py`** — extended
  (non-breaking) with `--mode offload`, a sync-on-event-loop vs.
  `asyncio.to_thread`-offloaded comparison under concurrent synthetic
  workers. Default `--mode smoke` behavior (insert/claim/complete) is
  unchanged.

Not touched: `core/crawler_manager.py` (it already passes the *raw*
`self.media_database` through `crawler_args["media_database"]`, exactly
like it already did for `frontier` — no change was needed for the adapter
to reach every engine), `storage/redis_media_evidence_store.py` /
`storage/sqlite_media_evidence_store.py` (no Redis keys, Lua scripts, or
data structures changed), `storage/media_evidence_store.py` (the
`MediaEvidenceStore` protocol stays fully synchronous), the frontier, claim/
lease/heartbeat logic, crawler scheduling, search engines, Tor, Selenium/
Playwright fetch/routing logic itself, fingerprinting, `config.yaml` (no
new knobs — there is nothing to tune; the adapter has no configuration
surface).

## Execution boundary: how blocking Media Evidence calls are isolated from asyncio

`core/media_evidence_executor.py`'s `AsyncMediaEvidence` wraps any
`MediaEvidenceStore`-conforming object and exposes `async` counterparts for
the two operations actually called from the crawl-time hot path:

```python
async def record_media_link(self, *, url, ..., priority=10) -> str:
    return await asyncio.to_thread(self._store.record_media_link, url=url, ..., priority=priority)

async def record_manifest_variants(self, asset_id, variants) -> None:
    await asyncio.to_thread(self._store.record_manifest_variants, asset_id, variants)
```

- **Unconditional offload — the one deliberate difference from
  `AsyncFrontier`.** `AsyncFrontier` skips the thread hop entirely for the
  local `URLFrontier` backend (pure in-memory, genuinely non-blocking).
  Media Evidence has no such backend: `SQLiteMediaEvidenceStore` (the
  local/dev backend) still does real synchronous `sqlite3` disk I/O, not
  in-memory work. `AsyncMediaEvidence` therefore always offloads, for both
  backends — there is nothing to gate on `isinstance(...)`.
- **Narrow scope, deliberately.** Only `record_media_link`/
  `record_manifest_variants` have async counterparts. Every other
  `MediaEvidenceStore` method (`claim_next_fingerprint_job`,
  `renew_job_lease`, `complete_fingerprint_job`, `fail_fingerprint_job`,
  `list_media_assets`, `list_observations`, `list_manifest_variants`,
  `get_fingerprint_jobs`, `get_status_counts`, `clear`, `close`) is a
  startup/shutdown/CLI/future-fingerprinter-worker operation, not part of
  the per-page crawl hot path this audit finding was about — grep-confirmed
  (`grep -n "self\.media_database\."` across `crawler/*.py`) that no
  crawler engine calls anything else. `core/crawler_manager.py` still calls
  `self.media_database.clear()`/`.close()` directly on the raw synchronous
  store, unchanged.
- **Idempotent**, same guard as `AsyncFrontier`: wrapping an
  already-wrapped `AsyncMediaEvidence` reuses the same underlying store
  instead of nesting adapters (nesting would hand a coroutine function to
  `asyncio.to_thread`, which is wrong). This is what lets `HybridCrawler`
  construct its six sub-engines from the same raw `media_database` without
  extra bookkeeping, exactly as it already does for `frontier`.
- **`None` stays `None`.** `AsyncMediaEvidence(media_database) if
  media_database is not None else None` — every existing `if not
  self.media_database: continue` / `if self.media_database:` guard across
  all seven engines is unaffected by this change; Media Evidence disabled
  (`config.crawler.storage.enable_media_evidence: false`) still behaves
  exactly as before.
- **Bounded threads**, same mechanism as `AsyncFrontier`:
  `asyncio.to_thread` reuses the event loop's shared default
  `ThreadPoolExecutor` (`min(32, os.cpu_count() + 4)` by default) — no
  per-call thread, no new executor.
- **Error semantics preserved exactly.** `asyncio.to_thread` propagates the
  underlying exception's type and message unchanged, it does not wrap or
  swallow. Every call site's existing exception handling therefore behaves
  identically to before: `worker()`'s `try: await
  self.media_database.record_media_link(...) except Exception as exc:
  logger.debug(...)` blocks still catch the same exceptions and still log
  at the same (`debug`) level (the audit's §10 finding that this is
  debug-only, not warning/info, was explicitly out of scope for this step —
  see "Deliberately out of scope" below); `async_crawler.py`/
  `http_crawler.py`/`tor_crawler.py`'s `fetch()` direct-response path still
  has no dedicated try/except around the call, so a failure there still
  falls through to the surrounding fetch-retry loop's `except Exception`
  exactly as before. No retry semantics changed anywhere.
- **Not applied to `MediaEvidenceStore` itself.** `storage/
  media_evidence_store.py`'s Protocol stays fully synchronous, unchanged —
  `AsyncMediaEvidence` is a separate, optional adapter crawler code opts
  into at construction time, same relationship `AsyncFrontier` has to
  `Frontier`.
- **Not a `redis.asyncio.Redis` migration**, and SQLite storage was not
  redesigned. `storage/redis_media_evidence_store.py` and
  `storage/sqlite_media_evidence_store.py` are byte-for-byte unchanged —
  same Redis-first production architecture (Redis is production
  infrastructure, SQLite is development-only and not a fleet-wide mirror,
  per `media-evidence-redis-design.md`, "Architecture Boundaries"); this
  step does not introduce a Redis+SQLite dual-write path or otherwise touch
  that boundary.

## Relationship to the frontier fix

| | `AsyncFrontier` (Step 4) | `AsyncMediaEvidence` (this step) |
|---|---|---|
| Wraps | `Frontier` protocol | `MediaEvidenceStore` protocol (2 of 13 methods) |
| Offload rule | Conditional — skipped for local `URLFrontier` | Unconditional — no backend is guaranteed non-blocking |
| Construction site | Each crawler engine's `__init__`, from the raw `frontier` param | Each crawler engine's `__init__`, from the raw `media_database` param |
| Idempotent re-wrap | Yes | Yes |
| Mechanism | `asyncio.to_thread` | `asyncio.to_thread` |
| Protocol changed? | No | No |

Both adapters now exist side by side; every crawler engine holds one of
each. A future third synchronous-boundary problem (were one found) would
follow the same shape.

## Tests run/results

```
tests/media_evidence_executor_test.py                 15 passed   (new)
tests/media_evidence_test.py                           3 passed   (SQLite backend, regression check)
tests/redis_media_evidence_store_test.py              18 passed   (Redis backend, regression check)
tests/media_evidence_multiprocess_test.py              3 passed   (regression check)
tests/crawler_test.py, hybrid_crawler_test.py,
  scrapling_crawler_test.py, extra_crawlers_test.py,
  crawler_heartbeat_integration_test.py,
  crawler_manager_recovery_test.py,
  crawler_manager_seed_failure_semantics_test.py        35 passed, 2 skipped
                                                          (browser tests, gated by
                                                          RUN_BROWSER_CRAWLER_TESTS=1)

Full suite (tests/):                                   214 passed, 2 skipped, 0 failed
RUN_BROWSER_CRAWLER_TESTS=1 tests/extra_crawlers_test.py: 5 passed
  (real Playwright + real Selenium execution, unaffected)
```

One pre-existing, unrelated flaky test
(`frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`)
failed once in isolation and passed in the full-suite run — `core/frontier_executor.py`
was not touched by this step; not investigated further here.

Also ran an ad hoc live-browser smoke check (not part of the pytest suite):
a real `PlaywrightCrawler.fetch()` against a real local page with a
`<video>` tag and a real `SQLiteMediaEvidenceStore`, confirming the awaited
`_route_request` network-interception path records a real media asset
end-to-end through a live browser.

### New tests, what they prove (`tests/media_evidence_executor_test.py`)

- `TestOffloadProof` — every `record_media_link`/`record_manifest_variants`
  call, against **both** backends (unlike `AsyncFrontier`'s tests, which
  only need to prove this for Redis), executes on a different OS thread
  than the event loop (thread-identity spy, same technique as
  `frontier_executor_test.py`); a slow synthetic write does not starve a
  concurrently-scheduled coroutine (`test_a_slow_media_write_does_not_block_a_concurrent_task`);
  concurrent calls stay within `asyncio.to_thread`'s bounded shared pool
  (≤32 distinct threads for 50 concurrent calls).
- `TestRecordMediaLinkThroughAdapter` / `TestRecordManifestVariantsThroughAdapter`
  — round-trip correctness through the adapter, both backends, including
  duplicate-discovery dedup.
- `TestExceptionsPropagateForNonFatalHandling` — `InvalidMediaURLError` and
  a synthetic `MediaEvidenceUnavailable` both propagate through the adapter
  unchanged (type and message), and the adapter/event loop remain usable
  after a caught exception — proving the crawler's existing non-fatal
  `except Exception as exc: logger.debug(...)` pattern keeps working
  unmodified.
- `TestSqliteLocalModeIsNotBroken` — a multi-record round trip against
  `SQLiteMediaEvidenceStore` through the adapter (`--sql` / `frontier.type:
  sqlite` development mode).
- `TestAsyncMediaEvidenceIdempotency` — wrapping twice reuses the same
  underlying store; `None` stays `None`.

## Benchmark results (`tests/benchmarks/media_evidence_benchmark.py --mode offload`)

New `run_offload_comparison()`: synthetic concurrent workers (default 25,
matching `crawler.concurrency`'s default) each process synthetic pages
(default 20/worker, 3 media links/page) via either a direct synchronous
`record_media_link` call inside an `async def` (reproducing the pre-fix
bug exactly) or the same call through `AsyncMediaEvidence`. A concurrent
ticker task probes event-loop responsiveness every 10ms throughout each
phase. No HTTP/network fetch is involved — Media Evidence overhead is
isolated from fetch latency entirely; synthetic/local Redis data only, own
namespace, cleared before and after.

**Local Redis (sub-millisecond loopback round trip), 25 workers × 20 pages × 3 links = 1500 ops:**

| | sync-on-loop | async-offloaded |
|---|---|---|
| Elapsed | 0.597s | 0.698s |
| Throughput | 2513 ops/s | 2149 ops/s (−14%) |
| Event-loop ticks sampled | **0** (entire 597ms) | 52 |
| Scheduling delay (mean / p99) | n/a — loop never ran | 3.3ms / 10.5ms |

**Same shape, `--artificial-latency-ms 2` (250 ops, approximating a
non-loopback Redis — still synthetic, not a real network fetch):**

| | sync-on-loop | async-offloaded |
|---|---|---|
| Elapsed | 2.183s | 0.382s |
| Throughput | 344 ops/s | **1965 ops/s (+5.7×)** |
| Event-loop ticks sampled | **0** (entire 2.18s) | 31 |
| Scheduling delay (mean / p99) | n/a — loop never ran | 2.1ms / 7.5ms |

RSS overhead: negligible (+1–2MB, thread-pool threads). CPU tracks with
work actually completed per wall-clock second in each phase, not a
separate cost.

**Reading these honestly** (per the task's explicit instruction not to
claim a throughput win from `asyncio.to_thread` merely because it exists):

- The **0 ticks sampled** result is the direct, mechanism-level proof: for
  the *entire* sync-on-loop phase duration in both runs, the responsiveness
  probe never got to run even once — the event loop was completely
  unavailable to any other coroutine (every concurrent fetch, the
  scheduler, the recovery task) for that whole window.
- On trivial local-loopback Redis, offloading has **no throughput benefit
  and a small cost** (thread-hop overhead) — this is the expected, honest
  result, and matches the audit's own caution (§18: magnitude was
  "confirmed mechanism... not benchmarked").
- Once per-call latency is non-trivial (2ms, a closer proxy for
  network-attached Redis in the real Redis-first, multi-machine production
  architecture this crawler targets — see `media-evidence-redis-design.md`,
  "Architecture Boundaries"), offloading also unlocks **real concurrency**
  the blocking path structurally cannot have (every sync-on-loop call fully
  serializes with every other one, since nothing ever yields control),
  producing a 5.7× throughput gain in this synthetic scenario. This is not
  a claim about production throughput — it demonstrates the mechanism by
  which the fix could matter once Redis isn't instant, which was exactly
  the open question the audit flagged as unbenchmarked.

## Deliberately out of scope

Per the task's explicit instructions, none of the following were touched
in this step, even though several are documented elsewhere in
`fetch-extractor-audit.md`:

- `CrawlerRouter.prefers_browser()` wiring (§7/§13 P1 #2).
- Playwright's doubled sequential timeout (`goto` + `wait_for_load_state`)
  (§6/§9/§13 P2).
- Per-fetch timing instrumentation (§11/§13 P2).
- Tor-unavailable run-level signal (§4/§13 P2).
- Media-evidence-record failure log level (currently `debug`-only, §10/§13
  P2) — the adapter preserves this exactly as-is; raising it is a separate,
  one-line change not made here.
- Yandex cooldown granularity (§3/§13 P2).
- Dead-code removal (§13 P3).
- `tests/report.py --redis` stale key names (§13 P3, pre-existing, already
  documented in `system-architecture.md` §25).

Also out of scope by the task's explicit architectural constraints: no
Redis+SQLite dual-write path was introduced, Media Evidence storage itself
was not redesigned, and SQLite was not promoted to a fleet-wide mirror —
Redis remains the sole production backend, SQLite remains development-only,
exactly as `media-evidence-redis-design.md` specifies.

## Known limitations

- **Thread-pool contention at high concurrency.** Same caveat
  `frontier-step4.md` recorded for `AsyncFrontier`: each offloaded call
  costs a thread-pool hop on top of the actual I/O, and at very high
  `crawler.concurrency` (well above the 25 used in this step's benchmark),
  concurrent `to_thread` calls from *both* adapters (frontier and Media
  Evidence) share the same bounded default executor and could start
  queueing behind each other. Not observed as a problem in testing; worth
  watching on a real high-concurrency run.
- **Benchmark's "no throughput win" result is local-loopback-specific.**
  The realistic-latency comparison (`--artificial-latency-ms`) is a proxy,
  not a measurement of actual fleet-wide Redis latency — an operator
  wanting a production-representative number should re-run
  `--mode offload` against a real non-loopback Redis instance.
- **The debug-only media-evidence-failure log level (audit §10) is
  unchanged.** A record failure is exactly as invisible at default `INFO`
  production logging after this step as it was before — this step only
  changed *where* the call runs, not its logging.
