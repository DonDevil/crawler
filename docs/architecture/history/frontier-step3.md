# Frontier Migration — Step 3 Implementation Notes

Status: **implemented**. Corresponds to `docs/architecture/frontier-adr.md` §13, migration
step 3 only:

> 3. Implement the Redis v2 keyspace + Lua scripts (§5) in `core/redis_frontier.py`,
>    unit-tested in isolation the same way `tests/redis_frontier_test.py` already does
>    (skip-if-no-Redis).

Read `docs/architecture/frontier-adr.md`, `docs/architecture/frontier-step1.md`,
`docs/architecture/frontier-step2.md`, the current `core/frontier.py`, `core/url_frontier.py`
(the behavioral reference), the pre-Step-3 `core/redis_frontier.py`, and
`tests/redis_frontier_test.py` before making any changes, per the task's explicit instructions.
No re-audit of the rest of the crawler was performed.

## Files changed

- **`core/redis_frontier.py`** (rewritten) — `RedisURLFrontier` now implements the `Frontier`
  protocol (`core/frontier.py`): `get_next_url()` returns `FrontierClaim | None`, `mark_visited`/
  `mark_failed`/`mark_skipped` take a claim and validate its token, `renew_claim` and
  `reclaim_and_promote` are new. Constructor gained `max_retries`, `base_backoff`, `max_backoff`,
  `lease_ttl`, `domain_scan_limit`, `reclaim_batch_size`, `terminal_meta_ttl_seconds`, all with
  defaults matching the local frontier's Step-1 values where the ADR doesn't specify a Redis-
  specific default (`domain_scan_limit=50`, `reclaim_batch_size=200` per ADR §5's `K`/batch-size
  proposals). Existing constructor parameters (`redis_host`, `redis_port`, `redis_db`,
  `rate_limit`, `url_database`, `namespace`) are unchanged, so `core/crawler_manager.py`'s
  construction call site needed no edits.
- **`tests/redis_frontier_test.py`** (rewritten) — the 7 existing tests adapted to the claim-based
  API (`claim.url`/`claim.token` instead of raw strings, `mark_visited(claim)` instead of
  `mark_visited(url)`), plus 9 new tests covering ADR §12's Redis-frontier requirements (see
  "Tests run" below).

Not touched, per the task's explicit scope: `core/frontier.py`, `core/url_frontier.py` (both
already correct from Step 1), `core/config.py` (`FrontierConfig` still has no `lease_ttl`/
`max_retries`/etc. knobs — wiring those is Step 4, alongside the recovery task that would consume
them), `core/crawler_manager.py`'s construction logic, all 6 crawler backends, `core/scheduler.py`,
`tests/frontier_test.py` (local frontier, re-run only as a regression check — untouched).

## Redis keyspace

Exactly as specified in ADR §5, namespaced under `{namespace}:` (default `crawler:`):

| Key | Type | Purpose |
|---|---|---|
| `{ns}:seq` | STRING (INCR) | Global monotonic sequence, breaks priority ties |
| `{ns}:urls:known` | SET | Permanent dedup memory (every URL ever accepted by `add_url`) |
| `{ns}:urls:visited` / `:skipped` / `:failed_permanent` | SET | Terminal-state membership |
| `{ns}:domain:{domain}:queue` | ZSET, score=`priority*1e6+seq` | Per-domain ready queue |
| `{ns}:domain:{domain}:next_time` | STRING (float) | Per-domain rate-limit gate, not floored |
| `{ns}:domain_heads` | ZSET, score = that domain's queue-head score | Cross-domain priority index (the Redis analogue of the local heap) |
| `{ns}:domains:active` | SET | Mirrors `domain_heads` membership, for debugging/status |
| `{ns}:inflight` | ZSET, score=`lease_expires_at` | Claimed-but-not-completed URLs |
| `{ns}:claim:{url}` | HASH `{token, attempt, domain, priority, claimed_at}` | The CAS record completion/renewal validate against |
| `{ns}:attempts:{url}` | STRING (int) | Durable attempt counter, survives the retry cycle |
| `{ns}:retry_scheduled` | ZSET, score=`not_before` | Backoff holding area for retryable failures |
| `{ns}:meta:{url}` | HASH `{source_query, domain, priority, first_seen}` | Descriptive metadata needed to requeue on promotion/reclaim |

`urls:queued` does not exist as a set — `queued` count is derived arithmetically (see
"Performance characteristics"), matching the ADR's note that queued membership is derivable
rather than tracked directly.

## Claim algorithm

`get_next_url()` generates a `uuid4().hex` token client-side, then calls the `claim_next` Lua
script:

1. `ZRANGE domain_heads 0 K-1` — up to `K` (`domain_scan_limit`, default 50) candidate domains in
   priority order.
2. For each candidate, in order: if its queue is empty (stale `domain_heads` entry), self-heal by
   removing it and move on. Otherwise check `domain:{d}:next_time`; if rate-gated, **leave it in
   `domain_heads`** and try the next candidate — this is what lets a lower-priority-but-eligible
   domain yield work instead of being blocked behind a gated top-priority domain.
3. On the first eligible candidate: `ZREM` its queue head, resync `domain_heads`/`domains:active`
   to the new head (or remove the domain if the queue is now empty), `SET` `next_time`,
   `INCR attempts:{url}`, `HSET claim:{url}` with the token/attempt/domain/priority, `ZADD inflight`
   with the new lease expiry, and return the claim (including `source_query`, read from
   `meta:{url}` in the same script so no extra round trip is needed).
4. If none of the `K` candidates are eligible, return `nil` — the caller (`get_next_url`) returns
   `None`, exactly like the ADR's "may still exist but rate-gated" case.

All of the above is **one Lua script, one round trip**, and all timestamp arithmetic uses Redis
server time (`TIME` command) rather than a client-supplied timestamp — see "Known limitations /
design notes" for why this mattered in practice.

`get_next_url()` then checks the claimed URL against `URLUtils.is_blacklisted` in Python (a second
line of defense matching Step 2's crawler-backend behavior, for a URL that became blacklisted
after being queued); on a hit it calls `mark_skipped` and retries `claim_next`, bounded by
`_MAX_BLACKLIST_SKIPS_PER_CALL` (200) as a safety valve.

Completion (`mark_visited`/`mark_failed`/`mark_skipped`) shares one `complete_claim` script:
`HGET claim:{url} token`; mismatch returns `'stale'` (a no-op, logged, never applied — this is the
mechanism that stops a slow, since-reclaimed worker from corrupting a newer worker's claim);
match removes the claim record and `inflight` entry, then branches on outcome — `visited`/`skipped`
add to the corresponding terminal set and delete `attempts`/`meta`; `failed` reads `attempt` from
the (about-to-be-deleted) claim hash and either `ZADD`s `retry_scheduled` with
`backoff = min(base_backoff * 2^(attempt-1), max_backoff)`, or finalizes to `failed_permanent` if
`attempt >= max_retries`.

`renew_claim` is a third script: validate token, `ZADD inflight` with a fresh expiry, return the
new expiry or `nil` if the claim is no longer current.

`reclaim_and_promote(batch_size)` is a fourth script, implemented but **not wired to run
automatically** (see "Known limitations"). In one round trip it: (a) reads up to `batch_size`
expired `inflight` entries via `ZRANGEBYSCORE ... -inf now LIMIT 0 batch_size`, applies the same
attempt-vs-`max_retries` decision as `complete_claim`'s failed branch to each, and (b) reads up to
`batch_size` due `retry_scheduled` entries the same way and promotes each back into its domain
queue (reading domain/priority from `meta:{url}`), resyncing `domain_heads`/`domains:active`.
Returns `(reclaimed, requeued)` counts.

## State transitions

Matches ADR §2 exactly — `{QUEUED, INFLIGHT, RETRY_SCHEDULED, VISITED, SKIPPED, FAILED_PERMANENT}`,
every `known` URL in exactly one bucket at any instant:

- `add_url` → QUEUED (rejected if already `known`).
- `get_next_url` → INFLIGHT (claim issued, attempt counter incremented).
- `mark_visited` → VISITED (terminal).
- `mark_skipped` → SKIPPED (terminal, never retried).
- `mark_failed`, `attempt < max_retries` → RETRY_SCHEDULED; `reclaim_and_promote`'s phase (b),
  once `not_before` has passed → back to QUEUED.
- `mark_failed`, `attempt >= max_retries` → FAILED_PERMANENT (terminal).
- `reclaim_and_promote`'s phase (a) (lease expired, claim never completed) applies the identical
  attempt-vs-`max_retries` decision as `mark_failed` → RETRY_SCHEDULED or FAILED_PERMANENT.

`get_status_counts()` returns the six-bucket partition (see "Performance characteristics" for how
`queued` is computed without enumerating per-domain queues).

## Concurrency guarantees

- **No double-claim.** `claim_next` is a single Lua script; Redis executes scripts atomically
  (single-threaded), so two concurrent `get_next_url()` calls are strictly serialized at the
  server — there is no interleaving in which both could pop the same domain-queue head.
  `tests/redis_frontier_test.py::test_get_next_url_no_duplicates` and
  `test_concurrent_claims_same_domain_never_duplicate` (8 threads racing 200 URLs on one domain)
  both assert this directly, including on claim `token` uniqueness, not just URL uniqueness.
- **Stale claims can never complete a newer claim.** Every completion/renewal call validates
  `claim.token` against the live `claim:{url}` hash; once a claim is reclaimed (or already
  completed), its hash either no longer exists or holds a different token, so the stale caller's
  `mark_visited`/`mark_failed`/`mark_skipped`/`renew_claim` is rejected as a no-op.
  `test_stale_claim_rejected_after_lease_reclaim` and
  `test_renew_claim_extends_lease_and_fails_for_reclaimed_claim` cover this end-to-end: claim →
  force-expire the lease → `reclaim_and_promote` → a second worker claims the same URL with a new
  token/attempt → the first worker's late `mark_visited`/`renew_claim` is rejected → the second
  worker's completion succeeds.
- **Global priority and rate limiting coexist.** A rate-gated domain is skipped (not removed) in
  `domain_heads` during `claim_next`'s scan, so a lower-priority-but-eligible domain is never
  blocked behind it. `test_rate_limited_domain_does_not_block_lower_priority_eligible_domain`
  covers this directly; `test_priority_ordering_across_domains_via_domain_heads` confirms ordering
  is by score, not insertion order or domain identity.

## Performance characteristics

Per-operation round-trip/complexity accounting (also documented as docstrings on each method in
`core/redis_frontier.py`):

| Operation | Round trips | Complexity |
|---|---|---|
| `add_url` | 1 | O(1) |
| `get_next_url` | 1 (2 only on the rare blacklisted-while-queued path) | O(K) server-side Lua work, K=`domain_scan_limit` (default 50); never O(domains) round trips, never a SCAN |
| `mark_visited` / `mark_failed` / `mark_skipped` | 1 | O(1) |
| `renew_claim` | 1 | O(1) |
| `reclaim_and_promote` | 1 | O(batch_size), independent of total domain/URL count |
| `get_status_counts` / `pending_count` / `has_pending` | 1 (pipelined) | O(1) — `queued` is derived as `known - visited - skipped - failed_permanent - inflight - retry_scheduled` rather than summed over every domain queue |
| `clear` | O(keys/200) SCAN iterations | Testing/reset only, never on the claim/scheduling hot path |

`test_reclaim_and_promote_is_constant_round_trips_regardless_of_domain_count` asserts this
concretely: wraps `redis_conn.execute_command` with a counter and confirms `reclaim_and_promote`
issues the same number of client→Redis commands whether 3 or 60 domains are involved (asserted
≤2, guarding against ever regressing to the old SCAN-based O(domains) behavior).

## Known limitations / intentionally deferred

- **No background lease-recovery task.** `reclaim_and_promote` is implemented and directly
  callable/testable, but nothing in `core/crawler_manager.py` calls it periodically yet. Until
  Step 4 wires the asyncio recovery loop (ADR §7), an abandoned claim (crashed worker) sits in
  `inflight` until something calls `reclaim_and_promote` — there is no automatic sweep. This is
  the explicit Step 3 instruction ("do not implement the background lease-recovery task yet").
- **No heartbeat/renewal wiring.** `renew_claim` exists and is tested, but none of the 6 crawler
  backends call it (`run_with_heartbeat` from ADR §8 is not implemented). A legitimately slow
  fetch that outlives `lease_ttl` (default 90s) will have its claim reclaimed exactly as if the
  worker had crashed — there is no way yet for a live worker to prove it's still working. Deferred
  to Step 5 per the ADR's own migration order.
- **Retry promotion is not automatic.** Per ADR §7, retry-scheduled promotion for the Redis
  backend is a background-task responsibility (unlike the local frontier, which promotes lazily
  inside `get_next_url` since Step 1). Since that task isn't wired yet, a URL in `RETRY_SCHEDULED`
  stays there until something calls `reclaim_and_promote` — tests exercise this explicitly by
  calling it manually after each `mark_failed`.
- **Redis server clock is authoritative, not client clocks.** Every script computes `now` via the
  Redis `TIME` command rather than trusting a client-supplied timestamp. This was not just a
  design preference: the first version of this implementation passed `time.time()` from Python,
  and `test_get_next_url_no_duplicates` (run with `rate_limit=0`, 3 concurrent worker threads)
  failed intermittently — a worker's request could arrive at Redis *after* another worker's
  later-timestamped request had already set `next_time`, making `next_time > now` spuriously true
  even though the rate limit should have already been satisfied. Using server time removes this
  entire race and is also the correct choice for a distributed system where worker machines' local
  clocks may not be in sync. All five Lua scripts use this pattern.
- **`clear()` uses `SCAN`.** Explicitly out of scope for the "no SCAN" requirement, which is about
  the scheduling/claim hot path — `clear()` is documented as testing/reset-only, matching the
  `Frontier` protocol's own guarantee for that method.
- **`terminal_meta_ttl_seconds` defaults to immediate deletion** (`None`/`0`), per ADR §9's
  explicit statement that the retention window is a product decision, not a Step 3 correctness
  concern. The knob exists (constructor parameter) for whoever makes that call later.
- **`FrontierConfig` (`core/config.py`) is still not wired** with `lease_ttl`/`max_retries`/
  `base_backoff`/`max_backoff`/`recovery_interval`/`reclaim_batch_size` — `RedisURLFrontier`'s
  constructor accepts all the relevant knobs (matching the local frontier's Step-1 constructor
  parameters), but `core/crawler_manager.py` doesn't pass them yet since there's no config surface
  for them. Bundled with Step 4 per the ADR's migration order.
- **Score overflow ceiling.** Domain-queue and `domain_heads` scores use `priority*1e6 + seq`
  (matching the ADR's literal formula) inside a Redis double (53-bit mantissa, safe to ~9e15).
  This is inherited from the pre-Step-3 implementation and the ADR itself; it would only become a
  problem after roughly 1e9 URLs have passed through a single namespace's `seq` counter, which is
  far outside any realistic single-run scope.

## Tests run

```
tests/redis_frontier_test.py   16 passed (3 consecutive full runs, no flakes)
tests/frontier_test.py          8 passed  (local frontier, regression check only — untouched)
tests/manager_test.py          10 passed  (regression check — construction call site unchanged)
```

New tests added (9), covering ADR §12's Redis-frontier requirements:

- `test_concurrent_claims_same_domain_never_duplicate` — 8 threads racing 200 URLs on one domain;
  asserts every URL and every token is claimed exactly once.
- `test_rate_limited_domain_does_not_block_lower_priority_eligible_domain` — the domain/rate-limit
  coexistence requirement called out explicitly in the task.
- `test_priority_ordering_across_domains_via_domain_heads` — 3 domains, later-added/higher-priority
  domain claimed first, verifying `domain_heads` ordering isn't insertion-order-dependent (this is
  the test the ADR notes "would have caught Revision 1's SCAN-order bug").
- `test_stale_claim_rejected_after_lease_reclaim` — claim → force-expire → reclaim → new claim →
  old claim's `mark_visited` rejected, new claim's succeeds.
- `test_renew_claim_extends_lease_and_fails_for_reclaimed_claim`.
- `test_mark_failed_retries_with_growing_backoff_then_fails_permanently` — 3 attempts then
  `failed_permanent`, confirmed via `get_status_counts`.
- `test_crash_injection_reclaim_requeues_below_max_retries` — claim never completed, lease force-
  expired, one `reclaim_and_promote` sweep, confirmed re-claimable with `attempt` incremented.
- `test_crash_injection_reclaim_terminalizes_at_max_retries` — same, but with `max_retries=1` so
  reclaim lands directly in `failed_permanent`.
- `test_reclaim_and_promote_is_constant_round_trips_regardless_of_domain_count` — the anti-SCAN
  regression guard described above.

All 7 pre-existing tests were kept (adapted to the claim-based API): `test_add_url_deduplication`,
`test_concurrent_worker_adds`, `test_get_next_url_no_duplicates`, `test_mark_visited_consistency`,
`test_rate_limit_per_domain`, `test_clear_frontier`, `test_namespace_isolation`.

`py_compile`/import checks passed for `core/redis_frontier.py` and `core/crawler_manager.py`
(confirms the unchanged constructor call site still type-checks against the new class).

## Deferred to Steps 4–6 (not started here)

- Step 4: wire the asyncio `_recovery_loop` background task in `core/crawler_manager.py` that
  periodically calls `reclaim_and_promote`, and add the corresponding `FrontierConfig` knobs
  (`lease_ttl`, `max_retries`, `base_backoff`, `max_backoff`, `recovery_interval`,
  `reclaim_batch_size`) to `core/config.py`/`config.yaml`.
- Step 5: implement `core/claim_heartbeat.py`'s `run_with_heartbeat` and thread it through all 6
  crawler backends' `worker()` methods so a legitimately slow fetch can renew its lease instead of
  being reclaimed as if crashed.
- Step 6: full multi-worker test suite plus a manual crash-injection soak test (`kill -9` a worker
  process mid-claim, confirm reclaim within `lease_ttl + recovery_interval`), and updating
  `REDIS_MULTIWORKER_SUMMARY.md`/`docs/DISTRIBUTED_SETUP.md` with the new config knobs and
  corrected performance/architecture description.

Not proceeding to Step 4 without explicit approval, per the task's instructions.
