# Phase 1 — Redis/SQLite Boundary Cleanup: Implementation Record

Status: **implemented and verified**. This document records what was
actually changed, in what files, why, and how it was verified — the
follow-through on the decision made in
[`redis-sqlite-boundary-decision.md`](redis-sqlite-boundary-decision.md)
(§11/§12 "Recommended architecture" / "Migration plan") and the evidence
gathered in [`sql-persistence-audit.md`](sql-persistence-audit.md). Read
those two first; this document assumes their findings and does not
re-derive them.

Scope, restated from the brief that drove this work: remove the redundant
SQLite URL-frontier mirror from Redis mode only. Do not implement a
Redis→SQLite fallback. Do not touch `MediaEvidenceDatabase`. Do not alter
SQL mode's own behavior. Stop after verification — no media-evidence
redesign, no fingerprinting.

---

## 1. What was removed

Redis mode's crawl-time hot path made 5 SQLite writes per URL before this
change (confirmed by the audits, re-confirmed by the benchmark in §6):

| # | Call site (before) | Nature |
|---|---|---|
| 1 | `RedisURLFrontier.get_next_url()` → `url_database.update_status(url, "pending")` | Embedded frontier mirror write |
| 2 | `RedisURLFrontier.add_url()` → `url_database.add_url(cleaned, status="queued")` | Embedded frontier mirror write |
| 3 | `RedisURLFrontier._complete()` → `url_database.update_status(claim.url, db_status)` | Embedded frontier mirror write |
| 4 | Every crawler engine's `worker()` → `url_database.add_url(url, status="pending")` | Duplicate of #1 |
| 5 | Every crawler engine's `worker()` → `url_database.update_status(url, status)` (terminal) | Duplicate of #3, and the source of the retry-status clobber bug (see §4) |

All 5 are gone from the Redis execution path. `RedisURLFrontier` no longer
has any notion of `url_database` at all — the constructor parameter, the
`self.url_database` attribute, and the three write sites (`core/redis_frontier.py`,
formerly lines ~28, ~73, ~90, ~112, ~457-458, ~520-521, ~600-607) were
deleted outright, not merely made conditional.

---

## 2. What was kept — and how SQL mode was protected

The naive reading of the brief's REMOVE list ("delete the worker-side
`add_url("pending")` and terminal `update_status` calls... across all
engines") would delete items #4/#5 unconditionally from all 7 engine files.
That would have been wrong: `core/url_frontier.py`'s `URLFrontier` (the
local, single-process SQL-mode frontier) **never writes `"pending"` or any
terminal status to `url_database` itself** — `mark_visited`/`mark_failed`/
`mark_skipped` only touch in-memory sets (`self.visited`, `self._skipped`,
`self._failed_permanent`). In SQL mode, items #4/#5 are not duplicates of
anything — they are the *only* place `"pending"`/`"visited"`/`"failed"`/
`"skipped"` ever get written to `url_database`, and that recording is
load-bearing for two things SQL mode already relies on today:

- `URLFrontier.add_url()`'s own crash-recovery dedup check,
  `self.url_database.is_visited(cleaned)` (`core/url_frontier.py:76`) —
  this can never return `True` for anything if nothing ever writes
  `status="visited"`.
- `CrawlerManager.load_unfinished_urls()` / `--unfinished`, which reads
  `url_database.get_urls_and_statuses(["queued", "pending"])`
  (`core/crawler_manager.py:311`) — SQL mode's own resume mechanism.

So items #4/#5 were **not deleted**; they were made conditional on which
frontier backend is actually active. Each of the 7 engines
(`crawler/{async,http,hybrid,tor,selenium,playwright,scrapling}_crawler.py`)
now computes, once, in `__init__`:

```python
self._sql_mode_mirror = url_database is not None and isinstance(self.frontier.raw, URLFrontier)
```

(`crawler/async_crawler.py:54`, and the equivalent line in each of the
other 6 files). `self.frontier.raw` is `AsyncFrontier`'s existing escape
hatch to the wrapped synchronous frontier object
(`core/frontier_executor.py:64-68`) — no new plumbing was needed. All three
call sites per engine (blacklist-skip write, pending write, terminal write
— `crawler/async_crawler.py:163,167,218`) were changed from
`if self.url_database:` to `if self._sql_mode_mirror:`.

Net effect: this single boolean, computed identically in all 7 engines,
correctly reproduces "remove the mirror from Redis mode" and "leave SQL
mode's behavior untouched" simultaneously, without any special-casing in
`CrawlerManager` and without altering `URLFrontier`/`URLDatabase`
themselves (the explicit PRESERVE constraint).

`CrawlerManager` (`core/crawler_manager.py`) needed exactly one change:
stop passing `url_database=self.url_database` into the `RedisURLFrontier(...)`
constructor call (it still passes it into both `URLFrontier(...)`
constructions — the explicit-`sqlite`-config branch and the
Redis-connection-failed fallback branch — and still passes it unconditionally
into every crawler engine's constructor, since the engines now self-gate).
`self.url_database` itself is still constructed unconditionally in
`CrawlerManager.__init__`, because it remains genuinely required for:
`clear_storage()`, the startup-seed durable-defer branch inside
`_make_seed_url_adder()` (`core/crawler_manager.py:275`, untouched),
`load_unfinished_urls()`, and shutdown status logging.

---

## 3. Files changed

| File | Change |
|---|---|
| `core/redis_frontier.py` | Removed `url_database` constructor param, attribute, import, and all 3 embedded mirror writes. -18 lines. |
| `core/crawler_manager.py` | Removed `url_database=self.url_database` from the `RedisURLFrontier(...)` call. -1 line. |
| `crawler/async_crawler.py` | Added `URLFrontier` import + `_sql_mode_mirror` computation; gated 3 call sites. |
| `crawler/http_crawler.py` | Same pattern. |
| `crawler/hybrid_crawler.py` | Same pattern (its own `worker()`; sub-engines it constructs get the same fix independently). |
| `crawler/tor_crawler.py` | Same pattern. |
| `crawler/selenium_crawler.py` | Same pattern. |
| `crawler/playwright_crawler.py` | Same pattern. |
| `crawler/scrapling_crawler.py` | Same pattern. |
| `tests/crawler_manager_seed_failure_semantics_test.py` | 3 assertions updated — see §5. |
| `tests/redis_sqlite_mirror_removal_test.py` | **New** — 2 regression tests, see §4. |

`git diff --stat` for the production code + updated test:
10 files changed, 66 insertions(+), 43 deletions(-).

---

## 4. Correctness fixes, and how they're proven

### 4.1 Retry-status clobber

Before: `RedisURLFrontier._complete()` correctly computed
`db_status="queued"` for a `retry_scheduled` outcome, but the worker's own
duplicate terminal write (item #5) ran immediately after and unconditionally
wrote `status="failed"` (it had no concept of `retry_scheduled`),
overwriting the correct value. This corrupted `url_database`'s view of a
URL Redis was still actively retrying — invisible to the live crawl (Redis
is authoritative there), but capable of hiding the URL from
`load_unfinished_urls()` on a **fresh process** started with `--unfinished`
while Redis's retry was still in backoff.

After: neither write exists in Redis mode (item #1/#3 deleted from
`RedisURLFrontier`; item #4/#5 gated off by `_sql_mode_mirror` being
`False`). `url_database` is not merely "correct" for this case — it is
**untouched**, which is a strictly stronger guarantee.

Proven by `tests/redis_sqlite_mirror_removal_test.py::test_retry_scheduled_url_is_not_written_to_sqlite_as_failed`:
runs a real `RedisURLFrontier` + `AsyncCrawler` against a server that always
returns HTTP 500, with `max_retries=3` so the first failure lands in
`retry_scheduled` (not `failed_permanent`). Asserts
`frontier.get_status_counts()["retry_scheduled"] == 1` and
`url_database.get_all_urls() == []`.

### 4.2 SQLite failure interrupting Redis discovery

Before: the discovered-links loop (`for link in links: await
self.frontier.add_url(...)`, `crawler/async_crawler.py:198-199` pre-change)
had no per-link `try`/`except`, and `RedisURLFrontier.add_url()`'s SQLite
mirror write (item #2) wasn't guarded either. A `sqlite3.OperationalError`
on link *N* of a page's discovered links aborted the loop — links *N+1*
onward were never even offered to Redis, and the outer exception handler
then marked the page's own claim as `failed`, turning a Redis-side success
into a reported crawl failure purely because of a local-disk fault on one
machine.

After: `RedisURLFrontier.add_url()` has no SQLite call left to fault on,
and in Redis mode the engine's own `url_database` calls are gated off
before they're ever reached. There is no code path left in Redis mode
where a `url_database` exception can interrupt discovery — not "unlikely
to," structurally cannot.

Proven by `tests/redis_sqlite_mirror_removal_test.py::test_url_database_failure_cannot_interrupt_redis_discovery`:
a `FailingURLDatabase(URLDatabase)` subclass that raises
`sqlite3.OperationalError` on every `add_url`/`update_status` call is wired
into a real Redis-mode `AsyncCrawler` crawling a page with 5 discovered
links. Asserts all 5 links reach Redis (`counts["queued"] == 5`), the page
is `visited`, `pages_failed == 0`, and — the direct proof of the new
boundary — `failing_db.write_calls == 0`.

---

## 5. Existing tests that had to change, and why that's not scope creep

`tests/crawler_manager_seed_failure_semantics_test.py` had 3 assertions
that encoded the *old* behavior of item #2
(`RedisURLFrontier.add_url()`'s mirror write) on the **healthy** (non-outage)
seeding path — e.g. `test_healthy_seeding_uses_exactly_one_call_per_url_no_wasted_retries`
asserted `url_database.get_urls_and_statuses(["queued","pending"])` equaled
the full seeded URL set after an ordinary, non-deferred `load_seed_urls()`
run. That assumption is exactly the mirror write this phase removes, and is
explicitly superseded by `redis-sqlite-boundary-decision.md` §11
("Nothing about Redis mode's crawl-time hot path should construct, open, or
write to any SQLite file except the narrow, bounded... startup-seeding
durable-defer path").

Updated (not deleted, not weakened): these 3 assertions now expect
`url_database` to stay **empty** after a healthy/accepted/rejected seed add,
with a comment explaining why. The tests in the same file that exercise the
**deferred-on-outage** branch of `_make_seed_url_adder` (`_SEED_ADD_*`
circuit breaker/retry tests) were untouched and still pass unmodified —
that mechanism is explicitly preserved, not part of this change.

---

## 6. Benchmark

Method: a real `CrawlerManager` (Redis mode, `async` engine), a real local
`aiohttp` server serving a closed 150-page link graph (4 links/page), a
`URLDatabase` subclass instrumented to count calls and time spent in them.
Run 3x against the working tree, then `git stash` (tracked files only, to
isolate the production-code diff) to get the pre-change tree, run 3x again,
then `git stash pop` to restore. Script:
`/tmp/.../scratchpad/redis_mode_benchmark.py` (throwaway, not committed).

| Metric | Before | After |
|---|---|---|
| Wall time (150 pages) | ~5.7s | ~5.6s |
| Throughput | ~26.3 pages/s | ~26.8 pages/s |
| `url_database` calls | **750** (exactly 5/URL — matches §1's table) | **0** |
| Time spent inside `url_database` calls | ~1.01s (~18% of wall time) | 0s |
| Errors | 0 | 0 |
| Correctness | 150 visited, 0 failed, 0 duplicate claims | identical |

Matches the audit's explicit prediction: no dramatic change to the
isolated-frontier ceiling (Redis was never the bottleneck —
`throughput-ceiling-audit.md` already showed Redis CPU drops to ~6% with
any realistic per-URL work in the loop), but a clean, complete elimination
of the measured SQLite call volume and the ~18%-of-wall-time cost and
tail-latency risk that came with it.

---

## 7. Verification summary

- Baseline (pre-change): 65-67/69 relevant tests passed deterministically;
  2 pre-existing timing/concurrency-sensitive tests against real Redis
  flake independent of this change (confirmed by repeated isolated runs
  before touching any code).
- Post-change: 159 passed, 2 skipped (optional-dependency skips, unrelated),
  full suite (`tests/` minus the manual `tests/benchmarks/` scripts) —
  same flaky test reproduces identically, confirmed via a `git stash`/`pop`
  round-trip that leaves the working tree in the same state either way.
- SQL mode: no automated test previously exercised `AsyncCrawler` +
  `URLFrontier` + `url_database` together end-to-end, so a manual smoke run
  was added to this doc's verification (not committed as a test, since it
  duplicates `tests/crawler_test.py`'s server-fixture pattern without new
  coverage) confirming `_sql_mode_mirror` evaluates `True` and
  `url_database` rows (`queued`→`visited` lifecycle) are byte-identical to
  pre-change behavior.
- Redis mode end-to-end: a second manual smoke run through the real
  `CrawlerManager` (not just the engine in isolation) confirmed
  `Database status counts: {}` after a full crawl — zero rows, zero
  writes, at the production composition root, not just inside the engine
  unit tests.

---

## 8. Remaining coupling and deferred items

- `MediaEvidenceDatabase` is untouched — still 100% SQLite, 100%
  local-per-machine, identical in both Redis and SQL modes. Out of scope
  per the brief; tracked as its own future phase in
  `redis-sqlite-boundary-decision.md` §5/§12 step 4.
- `CrawlerManager` still owns `url_database` unconditionally, for the
  startup-seed durable-defer mechanism (untouched) and lifecycle/reporting
  — this is the one legitimate remaining Redis-mode use, matching the
  decision doc's target architecture.
- **Behavioral note, not a regression**: `--unfinished` resume for Redis
  mode is now narrower than before in practice. Previously,
  `url_database` accumulated a `"queued"` row for every URL Redis ever
  accepted (via the now-removed item #2), so `--unfinished` could recover
  *any* URL Redis knew about, not just ones that failed to reach Redis at
  startup. After this change, `url_database` only ever contains URLs from
  the bounded startup-seed-defer path. Routine "Redis restarts, still has
  its own queued state" recovery is unaffected (Redis is self-durable
  across a crawler-process restart against the same instance) — this only
  changes the **disaster-recovery** case of total Redis data loss, where
  recoverable state is now limited to what was deferred at startup, not
  everything ever queued. This is the explicit, deliberate tradeoff
  `redis-sqlite-boundary-decision.md` §2/§11 describes, not an
  unintended side effect — but it's worth this callout for anyone who
  assumed `--unfinished` covered Redis's full queue.
- No changes made to: Lua scripts, claim/lease semantics, retries, rate
  limiting, domain scheduling, dedup, the recovery loop, Redis outage
  semantics, `domain_scan_limit`, or SQL mode's own code
  (`core/url_frontier.py`/`storage/url_database.py` are byte-for-byte
  unmodified).

**Stopped here per the brief.** No media-evidence redesign, no
fingerprinting work was started as part of this phase.
