# Redis Startup Recovery — Investigation (Audit Only)

Status: **investigation/design only — no code changed, no Redis state
modified.** Follow-up to
[`clear-db-redis-gap-audit.md`](clear-db-redis-gap-audit.md) §5, which
identified that `RedisURLFrontier.reclaim_and_promote()` sweeps abandoned
inflight leases and promotes due retries, but is only ever invoked by
`CrawlerManager._recovery_loop()` — a task that exists only while some
process's `run()` is actively executing. The originating incident: a real
overnight run was killed when VS Code crashed, leaving 228 inflight claims
and 73 retry-scheduled URLs un-reconciled in Redis until manually
inspected. This document establishes the correct startup-recovery design
before any implementation. Files read in full: `core/crawler_manager.py`,
`core/redis_frontier.py`, `core/config.py` (`FrontierConfig`),
`core/claim_heartbeat.py`, `tests/crawler_manager_recovery_test.py`,
`tests/redis_frontier_test.py`,
`docs/architecture/history/clear-db-backend-semantics.md`.

---

## A. What `reclaim_and_promote()` currently does

One Lua script (`core/redis_frontier.py:341-413`), one Redis round trip,
two independent phases, each bounded by `batch_size` (default 200,
`FrontierConfig.reclaim_batch_size`):

**Phase (a) — abandoned inflight claims.** `ZRANGEBYSCORE inflight -inf now
LIMIT 0 batch_size` finds claims whose lease already expired. For each: the
`claim:{url}` hash is read for `attempt`, then `ZREM`ed from `inflight` and
the claim hash `DEL`eted. If `attempt < max_retries`, the URL is `ZADD`ed
into `retry_scheduled` with the same exponential-backoff formula
`_complete_claim_script` uses for a normal failure. If `attempt >=
max_retries`, it's finalized straight into `failed_permanent` (`attempts:{url}`
and `meta:{url}` both hard-deleted — see §F for a minor asymmetry here).
**This phase never re-adds the URL to a domain queue.**

**Phase (b) — due retries.** `ZRANGEBYSCORE retry_scheduled -inf now LIMIT
0 batch_size` finds retry entries whose backoff has elapsed. For each: read
`domain`/`priority` from `meta:{url}`, `ZADD` into that domain's queue with
a **fresh** `seq` (so it re-enters at the back of its priority tier, not
its original position), and resync `domain_heads`/`domains:active`.

Because both phases are bounded by the same `batch_size` and phase (a)
feeds phase (b) only on the *next* call (a freshly-reclaimed retry isn't
promoted within the same invocation unless it was already due before this
call started), a single `reclaim_and_promote()` call is not guaranteed to
fully drain a large backlog, and a freshly-reclaimed retryable URL needs at
least one further call before it's claimable again. For the observed
228/73 backlog with the default batch size of 200, one call reclaims 200 of
the 228 inflight entries and promotes all 73 due retries; a second call is
needed to finish the inflight backlog and promote whatever phase (a) just
scheduled.

## B/C. Concurrency safety — is it safe for multiple systems to call this concurrently?

**Yes, unconditionally, by construction — no additional coordination
needed.** `reclaim_and_promote` is one Lua script, and Redis executes Lua
scripts atomically relative to every other command (single-threaded command
execution). Two systems calling it at "the same time" are, from Redis's
point of view, strictly sequential: script X's `ZRANGEBYSCORE` read and its
subsequent `ZREM`/`DEL`/`ZADD` writes for the URLs it selects happen as one
indivisible unit before script Y's `ZRANGEBYSCORE` can observe Redis state
at all. Since each `url` has at most one entry in `inflight` and one in
`retry_scheduled` (both are score-per-member structures), once X's script
has `ZREM`ed a URL from `inflight`, Y's script — running strictly before or
after, never interleaved — cannot also select and reclaim that same URL.
There is no read-modify-write window a second caller can land inside.

This is the same guarantee that already makes today's single-process
periodic `_recovery_loop` safe to leave running unattended; it doesn't stop
being true when a second process also calls the same method. No leader
election, distributed lock, or "only one system may run recovery" rule is
needed — every system (or every worker within a system, for that matter)
could call `reclaim_and_promote()` on its own timer with zero coordination
and the result would be identical to one system doing it alone, modulo
which caller's return value reports which count.

## D/E. When should startup recovery run, and is it separate from the periodic loop?

**Current gap, concretely:** `CrawlerManager.run()` (`core/crawler_manager.py:536-549`)
calls `self.prepare_frontier()` (adds seed/query/unfinished URLs — never
claims), then creates the `_recovery_loop` task via `asyncio.create_task`,
then `await self._crawler.run()` (starts workers, which call
`get_next_url()`). `_recovery_loop`'s first `reclaim_and_promote()` call is
the very first statement in its `while True` body — no `sleep` precedes
it — so in practice it runs at the next event-loop yield, essentially
concurrent with worker startup. But "essentially concurrent" is a
scheduling accident, not an ordering guarantee: nothing prevents a worker
from calling `get_next_url()` before that first sweep has completed, and
(per §A) a single sweep may not even finish reclaiming a large backlog. The
sharper problem: **if no crawler process is running at all** (the exact
incident scenario — everything crashed, nobody restarted the process for
hours), the periodic loop doesn't exist anywhere, so abandoned claims sit
un-reconciled until *some* process's `run()` eventually starts — at which
point recovery is racing worker startup rather than preceding it.

**Recommendation: add a one-shot startup-recovery sweep that runs to
convergence (or a bounded iteration/time cap) strictly before the crawler
begins claiming URLs**, i.e. before `await self._crawler.run()` — ordering
relative to `prepare_frontier()` doesn't matter for correctness (seeding
only touches `known`/domain-queue/`meta`/`seq`; recovery only touches
`inflight`/`retry_scheduled`/domain-queue-via-promotion), but doing
recovery first reads more naturally ("reconcile what's already there, then
add new work"). Concretely: loop calling `self.frontier.reclaim_and_promote(batch_size)`
until it returns `(0, 0)` or a safety cap is hit, gated by the same
`frontier_config.recovery_enabled and hasattr(self.frontier,
"reclaim_and_promote")` check `_recovery_loop`'s task-creation already
uses (the local frontier promotes due retries lazily inside its own
`get_next_url()` per ADR §10 and has nothing for a startup sweep to do).

**Yes, keep it separate from `_recovery_loop`,** sharing only the
underlying `reclaim_and_promote` primitive:
- The startup sweep is one-shot, synchronous-until-convergence, and exists
  to reconcile whatever a *previous* process (possibly the only one that
  ever ran) left behind before *this* process starts issuing claims.
- `_recovery_loop` is long-running and exists to reconcile crashes that
  happen *during this run* (a worker dying mid-run while the rest of the
  system stays up) — it must keep running unchanged after startup, and per
  §B/C it can safely coexist with any number of other systems' loops and
  with the one-shot sweep.
- Collapsing them into one mechanism would either force the periodic loop
  to loop-to-convergence on every tick (unbounded worst-case latency added
  to the crawl's steady-state cadence) or weaken the startup sweep to a
  single best-effort call (the exact race this fix is meant to close).

## F. Effect on inflight / retry_scheduled / attempts / fencing / domain queues

- **Inflight claims**: reclaimed only if genuinely lease-expired (score
  `<= now`); the claim hash is deleted and the URL either re-enters
  `retry_scheduled` (respecting the same backoff formula as a normal
  failure) or is finalized to `failed_permanent`.
- **Retry-scheduled URLs**: only those already due are touched; promoted
  back into their original domain's queue at their original priority, but
  with a fresh sequence number (loses exact original position within its
  priority tier — expected, not a bug; see §H).
- **Attempts**: the `attempts:{url}` INCR counter is untouched by
  phase (a) except in the failed-permanent sub-branch (deleted there, same
  as normal completion). This is what gives attempt continuity: the next
  real claim's `INCR` picks up exactly where the abandoned one left off, so
  `attempt` numbering after a reclaim is indistinguishable from a normal
  retry (`tests/redis_frontier_test.py::test_crash_injection_reclaim_requeues_below_max_retries`
  already confirms this at the frontier level).
- **Fencing tokens**: the abandoned claim's token is discarded along with
  its hash. A zombie worker that eventually calls `mark_visited`/`mark_failed`
  with the old `FrontierClaim` gets `'stale'` back (`_complete_claim_script`'s
  `HGET` returns nil after `DEL`) and is a guaranteed no-op — already
  covered by `test_stale_claim_rejected_after_lease_reclaim`. A startup
  sweep uses the exact same fencing path as the periodic loop; it doesn't
  introduce a new one.
- **Domain queues**: never touched by phase (a) directly. Only phase (b)
  writes to a domain queue, and only for URLs whose backoff had already
  elapsed by the time of that specific call — so, per §A, a URL abandoned
  and reclaimed in one sweep is not necessarily claimable again until a
  *subsequent* sweep's phase (b) runs.
- **One asymmetry worth flagging, not fixing here**: phase (a)'s
  failed-permanent branch always hard-deletes `meta:{url}` immediately,
  whereas `_complete_claim_script`'s `finalize_terminal` helper respects
  `terminal_meta_ttl_seconds` for the same transition via the normal
  completion path. Pre-existing in current code, unrelated to startup
  ordering, and out of scope per this task's brief ("do not redesign
  `reclaim_and_promote()` unless proven necessary") — noted as a follow-up
  candidate only.

## G. Can a newly started crawler steal a still-live claim?

**No.** Two independent guarantees combine to rule this out:
1. `reclaim_and_promote`'s `ZRANGEBYSCORE inflight -inf now` only ever
   selects entries whose lease score has *already* passed — a live claim
   whose lease hasn't lapsed is structurally invisible to it, regardless of
   how many systems call it or how often.
2. `core/claim_heartbeat.py` (already wired into every crawler engine, not
   just designed-but-unused) renews a live claim's lease at `lease_ttl / 3`
   (`default_heartbeat_interval`), giving ~2 renewal attempts of margin
   before a genuinely-alive worker's lease could ever reach the "expired"
   window. So under normal operation, a lease only actually expires when
   the worker holding it is truly gone (crashed, killed, network-partitioned
   long enough that heartbeat also can't get through) — at which point
   "reclaiming" it is exactly the correct behavior, not a steal.

A startup sweep changes nothing about this: it uses the identical
`reclaim_and_promote` script the periodic loop already uses, with the
identical lease-expiry precondition.

## H. Does recovery change rate limiting or priority semantics?

**Rate limiting: no.** Neither phase touches `domain:{domain}:next_time`
(only `_claim_next_script` sets it, on an actual claim). A domain that's
currently rate-gated stays rate-gated through a recovery sweep exactly as
if no sweep had run.

**Priority: preserved at the tier level, not at exact position.** Phase (b)
recomputes `score = priority * SCALE + seq` using the *original* `priority`
from `meta:{url}` (never mutated by recovery), so a recovered URL competes
correctly against everything else at its priority tier. It gets a *new*
`seq` (monotonically increasing, via the same `INCR ns:seq` fresh URLs
use), so it lands at the back of its own priority tier rather than
resuming its exact prior position — indistinguishable from a same-priority
URL discovered for the first time right now. This is expected, not a
regression to guard against.

---

## Recommended design (summary)

Add a `CrawlerManager` startup-recovery step — same backend gate as
`_recovery_loop`'s task creation (`frontier_config.recovery_enabled and
hasattr(self.frontier, "reclaim_and_promote")`) — that calls
`self.frontier.reclaim_and_promote(batch_size)` in a loop until it returns
`(0, 0)` or a bounded iteration/time cap is hit, executed **before**
`await self._crawler.run()` in `run()` (ordering relative to
`prepare_frontier()` is not correctness-sensitive). Keep `_recovery_loop`
unchanged as the ongoing, mid-run mechanism. No locking, leader election,
or single-owner assumption is required or should be added — §B/C show
concurrent callers across independent systems are already safe by
construction, which is also why running the same primitive once more at
startup adds no new risk even if another system is simultaneously running
its own startup sweep or periodic loop against the same namespace.

## Multi-system concurrency conclusion

Safe with zero added coordination. `reclaim_and_promote`'s atomicity is a
property of the Lua script + Redis's execution model, not of anything
`CrawlerManager` does — it holds identically whether the concurrent caller
is this process's own periodic loop, another worker in the same system, or
an entirely independent system's startup sweep or periodic loop. System A
and System B can both start at the same instant, both run their
loop-to-convergence startup sweep concurrently, and the shared namespace
ends up in the same state as if they'd run one after another — no URL is
ever reclaimed twice, requeued twice, or claimed by two workers from the
race itself.

## Proposed tests (not implemented)

All at the `CrawlerManager` level (`tests/crawler_manager_recovery_test.py`
already has the `_make_redis_config`/`_skip_if_redis_unavailable` fixtures
to build on); frontier-level equivalents for most of these already exist in
`tests/redis_frontier_test.py::TestClaimLifecycle` and are cited where
relevant as "already covered — this adds the `CrawlerManager` wiring
check."

1. **Crashed claim becomes recoverable after lease expiry** — construct a
   `RedisURLFrontier` directly to play "process #1," claim a URL,
   force-expire its lease (`redis_frontier_test.py`'s `_force_expire_lease`
   pattern), then construct a fresh `CrawlerManager` ("process #2") against
   the same namespace and assert its startup-recovery step alone (no test
   code calling `reclaim_and_promote` directly) leaves `inflight == 0` for
   that URL.
2. **Live claim is not prematurely reclaimed** — same setup but without
   force-expiring the lease; assert the claim (token, attempt, inflight
   membership) is byte-for-byte unchanged after a fresh `CrawlerManager`'s
   startup recovery runs.
3. **Startup recovery occurs before/at the correct point** — patch
   `_crawler.run` to a stub that immediately calls `get_next_url()` and
   records a shared order list; wrap `reclaim_and_promote` to append to the
   same list; assert the recovery call is recorded strictly before the
   first `get_next_url()` call.
4. **Two independent systems recovering simultaneously do not duplicate
   ownership** — seed one abandoned inflight claim (simulating a third,
   crashed system); construct two independent `CrawlerManager`/`RedisURLFrontier`
   pairs against the same namespace (System A, System B); run both
   startup-recovery steps concurrently (`asyncio.gather` or two threads);
   assert the URL is reclaimed exactly once and, once claimable again, is
   returned to exactly one of the two systems' `get_next_url()` callers —
   never both.
5. **Recovered URL receives correct attempt/fencing behavior** —
   crash-simulate at attempt 1, run startup recovery, claim again, assert
   `attempt == 2` and a new token; assert the original claim's
   `mark_visited`/`mark_failed` is a no-op afterward. (Frontier-level
   equivalent already covered by `test_stale_claim_rejected_after_lease_reclaim`
   — this confirms the `CrawlerManager` startup path doesn't bypass it.)
6. **Due retry is promoted correctly** — seed a `retry_scheduled` entry
   with a past-due score and matching `meta` (e.g. via `mark_failed` with
   `base_backoff=0`), run startup recovery, assert the URL is claimable
   again with its original domain/priority.
7. **Normal recovery loop still works** — re-run
   `tests/crawler_manager_recovery_test.py::TestRecoveryTaskBehavior`
   unchanged as a regression gate, plus one new assertion that
   `_recovery_task` is still created and still fires on its normal cadence
   even when a startup sweep already ran immediately before it (startup
   sweep must not disable or replace the periodic task).
8. **Startup recovery does not destroy queued work** — seed several
   `queued` URLs (never claimed) and one already-`visited` URL alongside
   one genuinely abandoned inflight claim; run startup recovery; assert
   the queued URLs and the visited set are byte-for-byte unchanged and only
   the abandoned claim was affected.

## Implementation plan (for the follow-up task, not this one)

1. Add a small `CrawlerManager` method (e.g. `_run_startup_recovery`) that
   loops `self.frontier.reclaim_and_promote(batch_size)` until `(0, 0)` or
   a cap, gated identically to `_recovery_loop`'s existing task-creation
   check.
2. Call it once in `run()`, before `await self._crawler.run()`.
3. Log reclaimed/requeued totals at completion (matching `_recovery_loop`'s
   existing `logger.debug` pattern).
4. Add the tests in the previous section; run only those plus
   `tests/crawler_manager_recovery_test.py` and
   `tests/redis_frontier_test.py::TestClaimLifecycle` as regression checks.
5. No change to `core/redis_frontier.py`, Lua scripts, lease TTLs, retry
   semantics, or heartbeat cadence.

## Unresolved questions (left for the implementation task or product/ops)

- What iteration/time cap should bound the startup loop-to-convergence, so
  a pathologically large or continuously-refreshed backlog (e.g. many other
  systems' claims expiring during our sweep window) can't unboundedly delay
  this process's own startup? This audit didn't need to pick a number.
- Should reclaimed/requeued totals be surfaced anywhere beyond a log line
  (e.g. a startup summary alongside the existing "Database status counts"
  log in `run()`'s `finally` block)?
- The `terminal_meta_ttl_seconds` asymmetry noted in §F (phase (a)'s
  failed-permanent branch always hard-deletes `meta:{url}` instead of
  respecting the configured TTL like `_complete_claim_script` does) — worth
  a dedicated look if it's ever observed to matter, not bundled into the
  startup-recovery fix.
- Phase (b)'s `if domain and priority then` guard means a retry-scheduled
  URL whose `meta:{url}` was somehow missing at promotion time is silently
  dropped (never requeued, never marked failed, no log). Should not occur
  in practice today (meta is only deleted at terminal finalization, and a
  retry-scheduled URL is by definition non-terminal), but is a silent-loss
  edge case worth keeping in mind if `terminal_meta_ttl_seconds`-style TTL
  logic is ever extended to non-terminal keys.
