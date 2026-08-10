# Frontier Migration — Step 2 Implementation Notes

Status: **implemented**. Corresponds to `docs/architecture/frontier-adr.md` §13, migration
step 2 only:

> 2. Update `core/scheduler.py` and all 6 crawler backends to the claim-based call sites (§11),
>    running against the local frontier only (existing default). Full existing test suite plus
>    §12's new local tests green before moving on.

Repository operated in: `/home/darkdevil/Desktop/anti_piracy/crawler` (confirmed via `git status`
at the start of this task; same repo used for Step 1). Read `docs/architecture/frontier-adr.md`
and `docs/architecture/frontier-step1.md` in full before making any changes.

No Redis v2, lease recovery, heartbeat, or Redis keyspace work was done — `core/redis_frontier.py`
is untouched, and nothing here wires up `renew_claim`, the asyncio recovery task, or new
`FrontierConfig` knobs. Those remain steps 3–6.

## Files changed

- **`core/scheduler.py`** — the (currently unused/stub) `Scheduler` class updated to consume
  `FrontierClaim` instead of raw URL strings; typed against `core.frontier.Frontier` instead of
  the concrete `URLFrontier` class.
- **`crawler/async_crawler.py`, `crawler/http_crawler.py`, `crawler/tor_crawler.py`,
  `crawler/playwright_crawler.py`, `crawler/selenium_crawler.py`, `crawler/scrapling_crawler.py`,
  `crawler/hybrid_crawler.py`** — `scheduler()` now puts `FrontierClaim` objects onto the work
  queue instead of URL strings; `worker()` consumes claims and completes them via the correct
  frontier method (see flow below). Each file's `frontier` constructor parameter is now typed
  `Frontier` and each `self.queue` is typed `asyncio.Queue[FrontierClaim]`.
- **`tests/manager_test.py`** — 4 assertions that compared `frontier.get_next_url()` directly to
  a URL string updated to read `claim.url` (these were the exact 4 failures flagged as expected
  in `frontier-step1.md`, now fixed since their underlying call sites are migrated).
- **`tests/crawler_test.py`** — added 3 new tests (worker exception, persistent fetch failure,
  worker cancellation mid-fetch — see "Tests added" below).
- **`tests/hybrid_crawler_test.py`** — added 1 new test (engine-escalation exhausted →
  `mark_failed` called exactly once, not left inflight).
- **`tests/extra_crawlers_test.py`, `tests/scrapling_crawler_test.py`** — no changes needed; both
  already asserted only `base_url in frontier.visited` / tested `fetch()` in isolation, which
  hold unchanged under the new contract.

Not touched: `core/frontier.py`, `core/url_frontier.py` (both already correct from Step 1),
`core/redis_frontier.py`, `core/config.py`, `core/crawler_manager.py`'s construction logic.

## Worker completion/error flow

Every one of the 7 files (6 backends + hybrid) now shares this identical shape in `worker()`:

```python
claim = await self.queue.get()
self._active_workers += 1
url = claim.url if claim else None

try:
    if claim is None:
        continue

    if URLUtils.is_blacklisted(url):
        self.frontier.mark_skipped(claim)          # terminal, never retried
        continue

    html, failure_reason = await self.fetch(...)
    status = "visited" if not failure_reason else "failed"
    # ... existing parsing / link-extraction / status bookkeeping, unchanged ...

    if status == "failed":
        self.frontier.mark_failed(claim, failure_reason or "")   # retry-or-terminal, frontier decides
    else:
        self.frontier.mark_visited(claim)

except asyncio.CancelledError:
    if claim is not None:
        self.frontier.mark_failed(claim, "worker cancelled")     # new — see below
    raise
except Exception as e:
    logger.error(f"Worker error for {url}: {e}")
    if claim is not None:
        self.frontier.mark_failed(claim, str(e))                 # new — closes the ADR §0 gap
finally:
    self._active_workers = max(0, self._active_workers - 1)
    self.queue.task_done()
```

This is the mechanical edit ADR §11 specifies, with one addition beyond what §11's diff shows
(explained below).

- **Blacklist during crawl** → `mark_skipped(claim)`. Previously this called `mark_visited(url)`,
  which the ADR §0 survey flagged as wrong (blacklist is a permanent skip, not a "successfully
  crawled" outcome). In practice this branch is rarely reached now: the local frontier's
  `get_next_url()` already filters blacklisted URLs before ever issuing a claim (Step 1), so this
  is a second line of defense for blacklist state that changes between claim issuance and the
  worker picking it up from its queue — kept for parity with the ADR's diff and because it's
  cheap insurance, not because it's the primary enforcement point anymore.
- **Fetch succeeds** → `mark_visited(claim)`.
- **Fetch fails (`failure_reason` set, transport-level retries in `fetch()` already exhausted)**
  → `mark_failed(claim, failure_reason)`. The frontier — not the crawler — decides retry vs.
  terminal from `claim.attempt` vs. `max_retries` (this is the whole point of centralizing that
  decision per ADR §0's "avoid duplicating retry logic 6×"). Previously this was
  `mark_visited(url)` unconditionally, meaning a failed fetch was indistinguishable from a
  successful crawl in frontier bookkeeping.
- **Unhandled exception** (parser error, media-DB error, anything `fetch()` didn't already catch)
  → `mark_failed(claim, str(e))` in the outer `except Exception` handler. This is the exact gap
  ADR §0 calls out: "today the URL is silently dropped from the frontier's bookkeeping." It no
  longer is — the claim gets resolved to `retry_scheduled` or `failed_permanent` immediately,
  not left dangling.
- **Worker cancellation (`asyncio.CancelledError`)** — this is the one exit path ADR §11's diff
  doesn't show, and auditing "every worker exit/error path" (this task's item 5) surfaced it: all
  7 files cancel their worker tasks during shutdown (`run()`'s `finally` block calls
  `task.cancel()` on every worker). If a worker is cancelled while a claim is bound (mid-`fetch`),
  the original code just re-raised, leaving that claim's bookkeeping stuck at `inflight` with no
  frontier call at all. Added `if claim is not None: self.frontier.mark_failed(claim, "worker
  cancelled")` immediately before the `raise`, so the claim resolves the same way an exception
  would, before cancellation propagates. This is plain synchronous bookkeeping local to the
  worker holding the claim — not lease recovery, not a background task, not Redis — so it stays
  in scope for this step. The two `except asyncio.CancelledError` blocks inside each `fetch()`
  method (the transport-level retry loop) were **not** touched — those stay bare `raise`, per ADR
  §0's "this loop is a transport concern and is out of scope."
- **`hybrid_crawler.py`'s engine escalation** — the `while plan:` loop already reused the same
  `claim`/token across every engine attempt (`async` → `scrapling` → `playwright` → ...) without
  ever calling `get_next_url()` again mid-loop, and only called the old unconditional
  `mark_visited(url)` once, after the loop, based on the final outcome across all attempted
  engines. That shape needed no structural change — only the same mechanical
  claim-token/`mark_failed`-vs-`mark_visited` substitution applied everywhere else. Verified by
  reading the full `worker()` method after editing (see below) and by the new
  `test_hybrid_crawler_marks_failed_once_after_exhausting_engine_escalation` test.

`core/scheduler.py`'s `Scheduler.run()` got the same `claim`-instead-of-`url` rename for
consistency (ADR §11 calls it out explicitly), though it remains dead code — nothing in the
codebase instantiates it; each crawler backend has its own internal `scheduler()` method instead.

## Tests added

- `tests/crawler_test.py::test_crawler_marks_failed_not_visited_on_persistent_http_error` — a
  local server that always returns HTTP 500; asserts the URL is never added to `frontier.visited`
  and lands in `failed_permanent` (frontier `max_retries=1`) with `inflight == 0`.
- `tests/crawler_test.py::test_crawler_worker_exception_fails_claim_instead_of_leaving_it_inflight`
  — injects a parser that raises `RuntimeError`, exercising the outer `except Exception` path;
  asserts the URL is never visited and the claim resolves to `failed_permanent`, not `inflight`.
- `tests/crawler_test.py::test_worker_cancellation_mid_fetch_does_not_leave_claim_inflight` —
  starts a worker on a claim, lets it enter a `fetch()` that hangs forever, cancels the worker
  task mid-flight (simulating shutdown), and asserts `get_status_counts()["inflight"] == 0`
  afterward — the test that specifically exercises the `CancelledError` fix above.
- `tests/hybrid_crawler_test.py::test_hybrid_crawler_marks_failed_once_after_exhausting_engine_escalation`
  — monkeypatches `_fetch_with_engine` to always fail regardless of engine, letting the
  escalation loop run to exhaustion; asserts the URL is never visited and lands in
  `failed_permanent`, confirming the single claim survives the whole escalation chain and is
  resolved exactly once.

## Tests run

```
tests/frontier_test.py        8 passed
tests/manager_test.py        10 passed   (was 6 passed / 4 failed after Step 1; now all green)
tests/crawler_test.py         4 passed   (1 pre-existing + 3 new)
tests/hybrid_crawler_test.py  6 passed   (5 pre-existing + 1 new)
tests/extra_crawlers_test.py  3 passed, 2 skipped (browser tests gated by RUN_BROWSER_CRAWLER_TESTS=1, unrelated)
tests/scrapling_crawler_test.py  2 passed
tests/redis_frontier_test.py  7 passed   (uses RedisURLFrontier, untouched by this change)

Total: 40 passed, 2 skipped, 0 failed
```

Re-ran the previously-unaffected suite as a regression check, unchanged:
```
tests/url_database_test.py, tests/url_utils_test.py, tests/discovery_test.py,
tests/parser_test.py, tests/main_cli_test.py, tests/media_evidence_test.py,
tests/streaming_manifest_test.py, tests/search_engine_test.py
26 passed, 0 failed
```

`tests/tor_test.py` and `tests/fingerprinter_queue_test.py` don't reference the frontier and were
not run, same as Step 1 (pre-existing, unrelated to this migration).

All files were also `py_compile`-checked and import-checked individually after each edit; no
syntax or import errors.

## Failures

None. Every test that was expected to break after Step 1 (per `frontier-step1.md`'s "expected,
scoped breakage" section) is now green, and no new failures were introduced.

## Can any claim still remain inflight unintentionally?

**Not under normal operation.** Every `worker()` exit path — blacklist skip, success, retryable
failure, permanent failure, unhandled exception, and task cancellation during shutdown — now
resolves the claim via `mark_skipped`/`mark_visited`/`mark_failed` before the worker moves on or
the exception propagates. This was verified both by code audit (every `except`/`continue`/fall-
through path in all 7 `worker()` methods was traced) and by the new tests above, which assert
`get_status_counts()["inflight"] == 0` after each failure mode.

**Residual, out-of-scope gap (expected, tracked for later steps):** a hard process crash (`kill
-9`, OOM-kill, power loss) still loses all in-memory frontier state, including any claim's
bookkeeping — indistinguishable from data loss on any in-memory structure, and no different from
this frontier's behavior before the migration. Per ADR §10, this is intentional for the local
frontier ("there is no crash-recovery scenario to build for locally... if the process dies, all
in-memory frontier state dies with it — no different from today, and no worse than before this
redesign"). Closing that gap for real (across process restarts, or for the Redis-backed
multi-worker case) is exactly what the asyncio recovery task, lease expiry, and `renew_claim`
wiring (ADR §7–8, migration steps 4–5) are for, and is explicitly out of scope for this step.
