# Frontier Migration — Step 1 Implementation Notes

Status: **implemented**. Corresponds to `docs/architecture/frontier-adr.md` §13, migration
step 1 only:

> 1. Introduce `FrontierClaim` + the `Frontier` protocol; update `core/url_frontier.py` to the
>    new contract (claim tokens, real `mark_failed`, `get_status_counts`) with no lease
>    machinery needed in-process. Lowest risk, fully testable without Redis, and it's the
>    behavioral reference everything else is checked against.

Repository operated in: `/home/darkdevil/Desktop/anti_piracy/crawler` (this project's git root;
confirmed via `git rev-parse --show-toplevel` before any edits).

## Files changed

- **`core/frontier.py`** (new) — `FrontierClaim` frozen dataclass and the `Frontier` Protocol,
  exactly as specified in ADR §1. Kept in its own module, separate from `core/url_frontier.py`,
  so the contract has one home shared by every future backend (local, Redis) instead of living
  inside whichever backend happens to be implemented first.
- **`core/url_frontier.py`** (rewritten) — `URLFrontier` updated to satisfy the new contract.
- **`tests/frontier_test.py`** (rewritten) — existing 4 tests adapted to the claim-based API,
  plus 4 new tests per ADR §12.

No other files were modified. `core/redis_frontier.py`, `core/config.py`, `core/crawler_manager.py`,
`core/scheduler.py`, and all 6 crawler backends are untouched, per the task's explicit scope
(migration step 1 only, no lease machinery, no backend call-site changes).

## What changed in `URLFrontier`

- `add_url` — dedup now checked against a single monotonic `_known` set (queued ∪ inflight ∪
  retry-pending ∪ terminal) instead of the old `visited ∪ _queued` pair. Behavior is unchanged
  for callers; this only closes the ADR's "known" gap described in §5's notes ahead of the
  Redis port, and there was no behavior to preserve differently here since the old checks were
  a subset of the same idea.
- `get_next_url` — now returns `FrontierClaim | None` instead of `str | None` (ADR §10,
  explicitly called out as the one intentional behavior change to the local frontier). Before
  returning a claim it: (1) lazily promotes any `retry_scheduled` URL whose backoff has expired
  back into its domain queue, then (2) runs the same priority-heap / rate-limit / blacklist-skip
  logic as before. Claim issuance (`_issue_claim`) generates a `uuid4().hex` token, increments
  the URL's durable attempt counter, and records the token as that URL's current owner.
- `renew_claim(claim)` — validates the token against the current owner and returns a refreshed
  claim, or `None` if the claim is stale. Per ADR §10 there is no lease-expiry/reclaim logic
  behind this locally (completion is always synchronous in-process); it exists purely for
  interface parity and token-validation symmetry with the eventual Redis backend.
- `mark_visited` / `mark_skipped` — now take a `FrontierClaim` and validate `claim.token` against
  the URL's current claim before applying. A stale token (already completed, or superseded by a
  later claim after a retry) is logged and ignored — no state change, no exception.
- `mark_failed(claim, error)` — new. Decides retry-vs-terminal from `claim.attempt` vs.
  `max_retries`: below the limit, schedules the URL for requeue at
  `now + min(base_backoff * 2**(attempt-1), max_backoff)` (`retry_scheduled` state, drained
  lazily at the top of the next `get_next_url()` call); at/above the limit, moves the URL to
  `failed_permanent`. This mirrors what `mark_failed` will do in the eventual Redis backend
  (ADR §4), so the two implementations can be tested against the same behavioral expectations.
- `get_status_counts()` — new. Returns the six-bucket partition from ADR §2:
  `{queued, inflight, retry_scheduled, visited, skipped, failed_permanent}`.
- `clear()` / `close()` — new. `clear()` wipes all in-memory state (testing/reset only).
  `close()` is a documented no-op: the local frontier owns no connections or resources —
  `url_database`, if provided, is owned and closed by the caller (`CrawlerManager`), not by the
  frontier, preserving the existing separation where `URLFrontier` never closes injected
  dependencies.

New constructor parameters, all with defaults matching the ADR's proposed values so existing
call sites (`URLFrontier(rate_limit=..., url_database=...)`) keep working unchanged:
`max_retries: int = 3`, `base_backoff: float = 5.0`, `max_backoff: float = 300.0`,
`lease_ttl: float = 90.0` (used only to stamp `FrontierClaim.lease_expires_at` — no expiry
checks are performed against it locally, per ADR §10). These are constructor-level, not wired
into `core/config.py`/`FrontierConfig` yet — that config plumbing wasn't required to make the
local contract correct and testable, and adding unused YAML knobs ahead of the backend that
needs them would be premature; it belongs with whichever later step actually consumes it
(Redis lease/recovery config, step 3–4).

Preserved behavior (per ADR §10 "preserve exactly"): per-domain rate limiting via
`domain_next_time`, global cross-domain priority via the `heapq` of `(priority, seq, domain)`,
blacklist-becomes-active-while-queued eviction, and the already-visited-in-database skip check.
All four original tests assert this still holds (see below).

## Test results

### `tests/frontier_test.py` — 8/8 passing

```
tests/frontier_test.py::test_frontier_marks_cleaned_urls_as_visited_and_prevents_requeue PASSED
tests/frontier_test.py::test_frontier_keeps_other_domains_available_when_one_is_rate_limited PASSED
tests/frontier_test.py::test_frontier_skips_urls_already_visited_in_database PASSED
tests/frontier_test.py::test_frontier_drops_queued_urls_when_domain_becomes_blacklisted PASSED
tests/frontier_test.py::test_mark_failed_retries_with_backoff_then_fails_permanently PASSED
tests/frontier_test.py::test_stale_claim_is_ignored_by_completion_methods PASSED
tests/frontier_test.py::test_status_counts_partition_every_known_url PASSED
tests/frontier_test.py::test_renew_claim_succeeds_for_current_owner_and_fails_after_completion PASSED
```

The first 4 are the pre-existing tests, adapted to read `claim.url` instead of comparing
`get_next_url()`'s result directly to a string — behavior asserted is otherwise identical to
before. The remaining 4 are new, covering ADR §12's required cases: retry/backoff up to
`max_retries` then permanent failure, stale-claim rejection (including the specific "claim from
before a retry" race), and the `get_status_counts` bucket-partition invariant.

### Full repository suite — expected, scoped breakage in unmigrated callers

This was the explicit tradeoff flagged by the ADR's own migration ordering (§13): step 1
changes `get_next_url()`'s return type, and step 2 (**not** part of this task) is what updates
`core/scheduler.py` and the six crawler backends to the claim-based call sites. Until step 2
lands, any code that still does `url = self.frontier.get_next_url()` and uses `url` as a raw
string is expected to break. Per the task instructions, those six backends and `scheduler.py`
were intentionally left untouched.

Confirmed blast radius, run individually with timeouts (some of these tests spin up local
HTTP servers / real crawl loops, so the full suite is not practical to run in one shot
independent of this change):

- **`tests/crawler_test.py`, `tests/hybrid_crawler_test.py`, `tests/extra_crawlers_test.py`,
  `tests/scrapling_crawler_test.py`** — construct a real `URLFrontier` and drive it through the
  actual crawler backends. Each backend's `worker()` still does `url = self.frontier.get_next_url()`
  and then uses `url` directly (blacklist check, `fetch(session, url, ...)`, etc.), so it now
  receives a `FrontierClaim` object instead of a string. Confirmed failure signature (from
  `crawler_test.py` and `hybrid_crawler_test.py`):
  ```
  ERROR | crawler.async_crawler:worker:206 - Worker error for FrontierClaim(url='...', token='...', ...): 'FrontierClaim' object has no attribute 'decode'
  ```
  Because the claim is never completed (the exception is caught by the backend's outer
  `except Exception` handler, which today makes no frontier call at all — this is exactly the
  gap ADR §0 calls out), the URL stays `inflight` forever, `has_pending()` never goes false, and
  the crawl loop hangs rather than failing fast. This is the precise scenario step 2's mechanical
  edit (ADR §11) and the outer-catch-all fix are meant to close — not something to patch around
  in step 1 without touching the six backends, which was out of scope here.
- **`tests/manager_test.py`** — 4 of 10 tests fail with plain `AssertionError`s (no hangs),
  because they compare `manager.frontier.get_next_url()` directly to a URL string:
  `test_prepare_frontier_query_only_skips_seed_files`,
  `test_prepare_frontier_unfinished_loads_only_resume_urls`,
  `test_prepare_frontier_prioritizes_onion_resume_urls`, `test_manager_can_ignore_blacklist`.
  These don't exercise `worker()`, so they fail cleanly rather than hanging. The other 6 tests
  in that file pass. Fixing these (swap to `claim.url` accessors) is bundled with step 2, since
  that's when `core/scheduler.py` and the backends they exercise get updated to match — updating
  just the assertions here now, while the manager wiring itself still forwards raw claims into
  unmigrated backends, wouldn't make the underlying integration correct.

Confirmed unaffected — passing, no changes in behavior:

```
tests/redis_frontier_test.py   (uses RedisURLFrontier, untouched)
tests/url_database_test.py
tests/url_utils_test.py
tests/discovery_test.py
tests/parser_test.py
tests/main_cli_test.py
tests/media_evidence_test.py
tests/streaming_manifest_test.py
tests/search_engine_test.py
```
33 tests total across these files, all passing.

`tests/tor_test.py` and `tests/fingerprinter_queue_test.py` don't reference the frontier at all
and were not run as part of this change (pre-existing, unrelated to this migration).

## Follow-up (tracked, not done here)

- Migration step 2 (ADR §13.2): update `core/scheduler.py` and all 6 crawler backends to the
  claim-based call sites (ADR §11), then fix `tests/manager_test.py`'s 4 broken assertions and
  re-verify `tests/crawler_test.py` / `tests/hybrid_crawler_test.py` / `tests/extra_crawlers_test.py`
  / `tests/scrapling_crawler_test.py` end-to-end.
- Redis v2 keyspace/Lua scripts, the asyncio recovery task, `renew_claim` wiring via
  `run_with_heartbeat`, and the deferred `lease_ttl`/`recovery_interval`/`reclaim_batch_size`
  config knobs are steps 3–6 and were not started, per the task's explicit "do not implement
  Redis v2 yet" instruction.
