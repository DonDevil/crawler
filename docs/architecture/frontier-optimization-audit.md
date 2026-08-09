# Frontier Optimization Audit (Step 6 Follow-Up)

Status: **audit only — no production code changed.** This document inspects the
implementation delivered by `docs/architecture/frontier-adr.md` steps 1-5 and the
Step 6 benchmark evidence, and determines what (if anything) is actually justified
to change. Every finding below is traced to a specific file/line and, where
practical, backed by a targeted measurement run against the real code in this
repo (not a proposed change) — see each finding's "Evidence" line.

Two isolated, read-only measurements were run as part of this audit (no files
were modified, no committed test/benchmark data was touched beyond a
`bench_probe_tmp`-namespaced Redis key range under `redis_db=2`, cleared
immediately after use):

1. Timed `URLUtils.clean_url()` / `is_blacklisted()` / their sub-calls in
   isolation (pure Python, no I/O beyond what the functions themselves do).
2. Timed the raw `RedisURLFrontier._claim_next_script` Lua call vs. the full
   `get_next_url()` call (which also runs `URLUtils.is_blacklisted`), against a
   local Redis instance already running on this machine.

Both are cited inline as "Measured" below.

---

## 1. Executive Summary

The Step 1-5 migration (claim tokens, lease/recovery, heartbeat, Redis Lua
keyspace) is **correct and well-tested** — every claim-safety, crash-recovery,
and heartbeat scenario in the Step 6 suite passes, and the design documented in
the ADR is faithfully implemented. **The Redis keyspace, Lua scripts, and
connection handling are not the bottleneck the benchmark numbers suggest.**

The single most important finding of this audit is that the ~140 URLs/sec
`add_url` ceiling and the ~6ms `get_next_url` claim latency reported in the
Step 6 benchmarks — for **both** the local and Redis frontiers, which is itself
the tell — are overwhelmingly caused by a bug in `utils/url_utils.py`'s
blacklist cache, not by Redis, Lua, connection pooling, or `asyncio.to_thread`.
`URLUtils._ensure_blacklist_file_exists()` calls `Path.touch(exist_ok=True)`
unconditionally on every single blacklist check; `touch()` always updates the
file's mtime, which immediately invalidates the very mtime-based cache
(`_reload_blacklist_if_needed`) that check is supposed to rely on — forcing a
full linear re-read and re-parse of `datasets/domain_blacklist.txt` (currently
1,463 lines) on **every** `add_url()` and `get_next_url()` call, for both
frontier backends, and via `get_link_priority()`, on every extracted link.
Measured directly against this repo: a raw Redis Lua claim round trip costs
**0.08ms**; the full `get_next_url()` call (identical Lua script, plus the
blacklist check) costs **6.07ms** — a 77x difference, and almost exactly the
"claim_operation_latency p50 ~6ms" the Step 6 benchmark reported as if it were
Redis's cost.

This reframes most of the "why does throughput plateau at 8 workers" question:
Redis's own CPU never approached saturation in any benchmark run (confirmed via
`redis_used_cpu_sys/user` in the resource-monitor samples), and the raw Lua
round trip is sub-millisecond on localhost. The 8→16 worker plateau in the
distributed (multiprocess) benchmark is explained by the **benchmark host's own
12 logical CPU cores** being oversubscribed by 16 independent OS processes each
paying the same per-call blacklist-reparse tax — a client-side CPU ceiling, not
a Redis-architecture ceiling.

A second, independent, and arguably more urgent finding: `is_blacklisted()` /
`get_link_priority()` are called **directly and synchronously inside every
crawler backend's `async def worker()`**, never wrapped in `asyncio.to_thread`
the way every `Frontier` call is. Given the bug above, this means every worker
now runs several milliseconds of blocking file I/O **directly on the event
loop thread** — once per claimed URL, and again once per extracted link (a
page with 50 links costs ~300ms of blocked event loop). This is a real gap in
the Step 4 "verify async correctness across the complete call graph" pass,
which audited every `Frontier`-protocol call site but not this adjacent one.

Beyond those two headline findings, this audit also confirms a P0-severity
Redis-failure-handling gap (`has_pending()`/`get_status_counts()` silently
return "nothing pending" on any Redis error, which the scheduler's shutdown
condition can misread as "crawl complete," causing premature shutdown during a
transient Redis outage), a silent-URL-loss vector in `add_url()` under the same
condition, a real (if narrow) domain-starvation scenario in `domain_scan_limit`
at higher domain cardinality than any existing test exercises, and several
smaller code-quality/duplication issues. The Redis keyspace's memory behavior
at 10K-1M URLs is sound by design and not a concern.

**Bottom line:** the Redis frontier itself does not need architectural rework.
The actual bottlenecks are two bugs/gaps in code adjacent to the frontier
(`URLUtils` blacklist caching, and its missing thread-offload in the crawler
workers), plus a handful of Redis-failure-handling gaps that matter far more
for correctness than for throughput.

---

## 2. Benchmark Evidence (as given, plus what this audit adds)

| Scenario | Reported | This audit's addition |
|---|---|---|
| Redis single-process, 1 worker | ~154 claims/sec | Matches `redis_20k_w1_norl.json` (153.67/s). Per-call cost dominated by `is_blacklisted()` (see §1), not Redis. |
| Redis single-process, 4 workers | ~121 claims/sec | Matches `redis_20k_w4_norl.json` (120.50/s) — *lower* than 1 worker. `process_cpu_percent` avg only rose from 94.7%→101.3% (not ~400%), meaning 4 OS threads did **not** get 4x parallelism — consistent with GIL + per-call Python-side cost (the blacklist bug) dominating over true I/O wait. |
| Distributed Redis, rate_limit=0, 2/4/8/16 processes | 197.3 / 292.7 / 352.2 / 339.7 claims/sec | `children_cpu_percent` (resource monitor) rose from avg 186% (2 workers) → 738% (8 workers, close to the host's 800%=8-core ceiling) → 1100% (16 workers, capped near the host's 1200%=12-core ceiling, `nproc`=12 on this machine). The 8→16 plateau/regression is client-process CPU oversubscription, not Redis. Redis's own `used_cpu_sys+user` stayed flat (~29→~53 over the whole multi-minute run) across all worker counts. |
| Claim latency p50/p99 (low concurrency) | ~6ms / ~7ms | Measured (this audit): raw Lua `claim_next` round trip = **0.08ms**. Full `get_next_url()` = **6.07ms**. The reported "Redis latency" is ~98.7% blacklist-check overhead. |
| Claim latency p50 (4 workers) | ~28ms | Matches `redis_20k_w4_norl.json`'s `claim_operation_latency.p50_s` (0.0281s) exactly. Consistent with 4 threads each paying the ~6ms blacklist tax with added GIL/scheduling contention, not Redis-side queueing (Redis CPU stayed low). |
| Insert throughput | Redis ~138-140/s, local ~143-145/s | Both **converge to nearly the same number despite one being in-memory and the other a network call** — the tell that a shared, backend-independent cost dominates both. Confirmed: `tests/benchmarks/common.py`'s `build_frontier()` never passes a `url_database`, so this isn't a SQLite-writer effect in the benchmark itself; it is the `URLUtils.clean_url()`/`is_blacklisted()` call inside both `add_url()` implementations. |
| Crash recovery, heartbeat, priority/rate-limit | All PASS | Confirmed by reading `benchmark/results/crash-recovery/*.json` and `heartbeat-endurance/*.json` directly; timelines match the ADR's designed state machine exactly, including the two-sweep reclaim→promote sequence (see §6). |

---

## 3. Confirmed Strengths

- **Claim-safety is genuinely solid.** `claim_next`, `complete_claim`,
  `renew_claim`, and `reclaim_and_promote` are each single Lua scripts —
  Redis's single-threaded script execution makes every one of them atomic
  server-side. No dual-claim, no lost update, no stale-completion-corrupts-newer-claim
  path exists in the code as written (`core/redis_frontier.py:190-417`), and
  the existing test suite (`tests/redis_frontier_test.py`, 16 tests) plus the
  Step 6 crash/heartbeat benchmarks exercise this directly and pass.
- **The `Frontier` protocol (`core/frontier.py`) is a clean, well-documented,
  backend-agnostic contract.** Both implementations (`URLFrontier`,
  `RedisURLFrontier`) satisfy it faithfully for the core claim/complete/retry
  lifecycle.
- **`AsyncFrontier` (`core/frontier_executor.py`) correctly and consistently
  offloads every `Frontier`-protocol call** for the Redis backend via
  `asyncio.to_thread`, confirmed by grep across all 7 crawler backends — every
  single `frontier.<method>()` call site is `await`ed, with no exceptions
  found (§7 below covers the one *adjacent*, non-`Frontier` gap).
- **The Redis keyspace design avoids the O(domains) SCAN problem it replaced.**
  `domain_heads` + the `K`-bounded scan in `claim_next` genuinely keeps
  `get_next_url` to one round trip regardless of domain count (excluding the
  starvation caveat in §4/§8.6).
- **Recovery is real and automatic**, not merely implemented-but-unwired: the
  `CrawlerManager._recovery_loop` task is gated correctly (only runs for
  backends implementing `reclaim_and_promote`, i.e. Redis) and is exercised end
  to end by `tests/crawler_manager_recovery_test.py`.
- **Retry/backoff logic is centralized in exactly one place per backend**
  (`mark_failed`/`_complete`), matching the ADR's explicit design goal of not
  duplicating retry-decision logic across the 7 crawler backends.
- **Redis memory design is sound.** Every ephemeral structure (`claim:{url}`,
  `attempts:{url}`, `meta:{url}`) is deleted at the exact terminal transition
  that makes it safe to delete; `terminal_meta_ttl_seconds` defaults to
  immediate deletion. Nothing in the Redis keyspace grows without bound beyond
  the two sets that are *supposed* to (`urls:known`, and one terminal set per
  URL) — see §5.

---

## 4. Confirmed Problems

Ordered by severity, not by discovery order.

### 4.1 — P0 — `has_pending()`/`get_status_counts()` treat a Redis error as "nothing pending," risking premature shutdown

**File:** `core/redis_frontier.py:637-656` (`has_pending`), `682-721`
(`get_status_counts`), `658-665` (`pending_count`).

Every one of these wraps its Redis pipeline in `try/except redis.RedisError`
and, on failure, returns `False` / an all-zero dict. `AsyncCrawler.scheduler()`
(`crawler/async_crawler.py:238-261`, and identically in all 6 other backends)
uses exactly this signal to decide the crawl is finished:

```python
if self.queue.empty() and self._active_workers == 0 and not await self.frontier.has_pending():
    idle_loops += 1
    if idle_loops >= 10:  # ~5 seconds of idle
        self._stop_event.set()
```

If Redis becomes unreachable (network blip, restart, timeout) at a moment when
the in-process queue is briefly empty and no worker is actively holding a claim
— an ordinary, frequent state, not a rare one — `has_pending()` returns
`False` for ~5 seconds' worth of polls and the **entire crawler shuts down**,
logging "No more URLs to crawl," while Redis in fact still holds the entire
remaining frontier. This is indistinguishable, from the crawler's point of
view, from a genuinely completed crawl. Nothing surfaces this as an error
condition; the only trace is the `logger.error` call buried inside
`has_pending`'s except block.

### 4.2 — P1 — `add_url()` silently drops newly-discovered links on any Redis error

**File:** `core/redis_frontier.py:427-460`.

```python
try:
    result = self._add_url_script(args=[...])
except redis.RedisError as e:
    logger.error(f"Redis error adding URL: {e}")
    return False
```

`worker()` (e.g. `crawler/async_crawler.py:199`) does
`await self.frontier.add_url(link, priority=...)` and never inspects the
return value. A transient Redis error at the moment a page's links are being
added means those links are gone — not retried, not queued locally, not
persisted anywhere else (the `url_database.add_url` write only happens if
`result` is truthy, so it's skipped too). There is no fallback queue and no
"add_url failure" counter anywhere in the codebase, so an operator would not
even know this happened without reading logs closely.

### 4.3 — P1 — completion (`mark_visited`/`mark_failed`/`mark_skipped`) failures are swallowed with no caller-visible signal

**File:** `core/redis_frontier.py:546-608`; every call site in
`crawler/*.py` (e.g. `async_crawler.py:206-208`).

`_complete()` catches `redis.RedisError`, logs, and returns the string
`"error"`. No caller anywhere checks this return value — `mark_visited`,
`mark_failed`, and `mark_skipped` are declared as returning `None` on the
`Frontier` protocol, so this failure signal is structurally unreachable to
callers even if someone wanted to check it. Concretely: a worker successfully
fetches a page, Redis blips during the `mark_visited` call, the worker logs
"Processed (...): url [visited]" and increments `_pages_crawled` — but the
frontier's Redis state still shows the URL `inflight`. It will eventually be
reclaimed after `lease_ttl` and **re-crawled from scratch**, wasting the
already-completed fetch, while the crawler's own progress counters have
already (incorrectly) counted it once. Not data loss, but a real,
silently-occurring duplicate-work and progress-accounting inconsistency.

### 4.4 — P1 — the blacklist cache re-parses the entire file on every check (root cause of §1's headline finding)

**File:** `utils/url_utils.py:253-255` (`_ensure_blacklist_file_exists`),
`484-511` (`_reload_blacklist_if_needed`), `514-535` (`is_blacklisted`).

```python
@classmethod
def _ensure_blacklist_file_exists(cls) -> None:
    cls._blacklist_path.parent.mkdir(parents=True, exist_ok=True)
    cls._blacklist_path.touch(exist_ok=True)   # <-- always bumps mtime
```

`is_blacklisted()` calls `ensure_blacklist_seeded()` (which calls
`_ensure_blacklist_file_exists()` then `_reload_blacklist_if_needed()`), then
calls `_reload_blacklist_if_needed()` a second time directly.
`_reload_blacklist_if_needed()`'s only cache-validity check is
`cls._blacklist_mtime_ns == stat.st_mtime_ns`. Because `Path.touch(exist_ok=True)`
unconditionally updates the file's mtime even when the file already exists
(standard `touch` semantics), **every single call invalidates its own cache
before checking it**, forcing a full re-read + line-by-line `urlparse()` of
the whole file.

**Evidence (measured against this repo, read-only):**
- `cProfile` over 200 calls to `add_to_blacklist` (which shares this reload
  path) showed 292,600 `urlparse()` calls — exactly 1,463 (the current
  blacklist file's line count) × 200 — confirming a full file re-parse on
  every single call.
- Isolated timing: `is_blacklisted()` / `clean_url()` cost **5.7-5.9ms/call**
  on this repo's current blacklist file; `normalize_url()` alone (no blacklist
  logic) costs **0.0057ms/call** — a ~1000x difference.
- Raw Redis Lua claim script alone: **0.08ms**. Full `get_next_url()`
  (identical script + one `is_blacklisted()` call): **6.07ms**.

This cost is paid **uniformly by both frontier backends** (both call
`URLUtils.clean_url()`/`is_blacklisted()` inline before any backend-specific
work), which is exactly why the Step 6 benchmark shows local and Redis
converging to nearly the same `add_url`/claim throughput — the shared, dominant
cost isn't in either frontier implementation. It also **grows over the life of
a crawl**: `should_auto_blacklist()`/`add_to_blacklist()` append new domains to
`datasets/domain_blacklist.txt` as suspicious domains are discovered (currently
1,463 lines from accumulated runs), so the per-call tax increases the longer a
crawl (especially an `--indefinite-run`) has been going.

### 4.5 — P0 — the same blocking, now-expensive blacklist check runs directly on the event loop thread in every worker

**File:** every `crawler/*_crawler.py`'s `worker()`, e.g.
`crawler/async_crawler.py:151` (`if URLUtils.is_blacklisted(url):`) and
`:199` (`URLUtils.get_link_priority(url, link, source_query)` — which itself
calls `is_blacklisted()` at `utils/url_utils.py:469` for links that aren't
same-domain, onion, or already known to be piracy-relevant).

Step 4's execution-boundary work (`core/frontier_executor.py`,
`docs/architecture/frontier-step4.md`) audited and fixed exactly one thing:
every `Frontier`-protocol call reachable from asyncio code. It did not (and
by its own stated scope, was not asked to) cover calls made *alongside*
those, and `URLUtils.is_blacklisted()`/`get_link_priority()` are called
directly inside `async def worker()`, never through `AsyncFrontier`, never
wrapped in `asyncio.to_thread`. Given §4.4, this is no longer a cheap
in-memory set lookup — it is several milliseconds of blocking filesystem I/O
(`stat()`, `open()`, full-file read, thousands of `urlparse()` calls)
executed **directly on the event loop thread**, once per claimed URL (the
blacklist re-check at the top of `worker()`) and once per extracted link (the
`get_link_priority` call inside the `for link in links:` loop, `async_crawler.py:198-199`).
A page yielding 50 extracted links pays this cost up to 50 times in a single
synchronous stretch — roughly 300ms of the event loop being unable to run any
other worker's `aiohttp` I/O, `asyncio.sleep`, or scheduler tick. This directly
undermines the concurrency Step 4 was built to protect, through a path that
Step 4 never looked at because it isn't part of the `Frontier` protocol.

### 4.6 — P1 — `domain_scan_limit` is a hard visibility cutoff, not just a rate-gate skip list

**File:** `core/redis_frontier.py:190-260` (`_claim_next_script`), scan bound
at line 204: `local candidates = redis.call('ZRANGE', domain_heads_key, 0, k - 1)`.

The Lua loop only ever considers the `K` (`domain_scan_limit`, default 50)
best-priority-scored domains currently in `domain_heads`. A rate-gated domain
*within* that window is correctly skipped in favor of the next eligible one
(tested: `test_rate_limited_domain_does_not_block_lower_priority_eligible_domain`).
But a domain ranked **outside** the top `K` by priority score is not skipped —
it is never examined at all, regardless of its rate-limit status. Because
rate-gating does not reorder `domain_heads` (only the inline eligibility check
uses `next_time`), a domain's rank is stable and determined purely by
`(priority, seq)`. If more than `domain_scan_limit` distinct domains have
active queues at the same time — plausible for this specific crawler, whose
seed files span piracy/torrent/streaming/darkweb site collections likely
totaling well over 50 distinct domains once link discovery is running — any
domain ranked 51st or worse by priority is **never claimed** until enough
higher-ranked domains fully drain out of `domain_heads` (i.e., their queues
empty entirely, which won't happen if they keep being replenished by ongoing
link discovery at a similar priority). This is a genuine starvation mechanism,
not merely a throughput cap, and none of the existing tests
(`priority_ratelimit.py`'s default scenario uses 3 domains;
`test_priority_ordering_across_domains_via_domain_heads` uses 3) exercise
enough simultaneous domains to reveal it.

### 4.7 — P2 — `BatchedDatabaseWriter` does not batch

**File:** `storage/async_database_writer.py:39-51`.

```python
def execute(self, sql, params=None) -> None:
    with self._lock:
        self._batch.append(WriteOperation(sql=sql, params=params))
        self._flush()   # <-- every single call flushes and commits immediately
```

The constructor accepts and stores `batch_size`, but `execute()` never checks
it — every call appends one operation and immediately flushes+commits. The
docstring even says so ("each operation is committed immediately instead of
waiting for a large batch to accumulate"), which contradicts the class's name
and its `batch_size` parameter. This means every `url_database.add_url()` /
`update_status()` call anywhere in the system (every claim, every completion,
every seed load) pays a full SQLite `commit()`. This is not the dominant cost
in the Step 6 benchmarks (`tests/benchmarks/common.py` never wires a
`url_database` into its frontiers at all — see §2), but it is a real,
always-on cost in the actual production path, since `CrawlerManager` always
passes `url_database=self.url_database` to both frontier constructors
(`core/crawler_manager.py:67,89,98`).

### 4.8 — P2 — Redis-backed completions write to `url_database` twice

**File:** `core/redis_frontier.py:585-592` (inside `_complete()`) *and*
`crawler/async_crawler.py:209-210` (inside `worker()`, after `mark_visited`/
`mark_failed` returns) — same pattern repeated in all 7 backends.

`RedisURLFrontier._complete()` already calls
`self.url_database.update_status(claim.url, db_status)` internally. Every
crawler backend's `worker()` then calls
`self.url_database.update_status(url, status)` again immediately afterward.
For the Redis frontier this is two SQLite writes (two commits, per §4.7) per
completion. The local frontier's `mark_visited`/`mark_failed`/`mark_skipped`
(`core/url_frontier.py:195-240`) never touch `url_database` at all — the
worker-level call is the *only* place it happens — so this is an accidental,
backend-inconsistent duplication rather than an intentional design choice.

### 4.9 — P2 — `recovery_enabled=False` + `type: redis` is a silently broken configuration

**File:** `core/crawler_manager.py:329` (`if frontier_config.recovery_enabled
and hasattr(self.frontier, "reclaim_and_promote"):`), `core/config.py:64`.

This is a legal config combination (nothing validates it). If set, no task
ever calls `reclaim_and_promote`: crashed workers' claims are never reclaimed,
retryable failures sit in `retry_scheduled` forever and never get promoted
back to `queued`, and (per §4.1) `has_pending()`/`pending_count()` will never
go to zero for those URLs — the crawler runs forever, believing work remains,
because it does, but none of it will ever move again. No warning is logged at
startup for this combination.

---

## 5. Redis Data Model / Memory Audit

Every key, exactly as implemented in `core/redis_frontier.py` (matches
`docs/architecture/frontier-step3.md`'s table, verified against the current
Lua scripts line-by-line — no drift found):

| Key | Type | Lifecycle | Grows unbounded? |
|---|---|---|---|
| `{ns}:seq` | STRING (INCR) | Forever (by design — monotonic sequence) | Yes, but a single integer; irrelevant until ~1e9 URLs in one namespace (§ADR note, still true) |
| `{ns}:urls:known` | SET | Added once per URL, never removed except `clear()` | **Yes, by design** — this is the permanent dedup memory |
| `{ns}:urls:visited` / `:skipped` / `:failed_permanent` | SET | Added at the URL's one terminal transition, permanent | **Yes, by design** — one of these three is where every URL ends up |
| `{ns}:domain:{d}:queue` | ZSET | Entries added by `add_url`/reclaim/retry-promotion, removed on claim | No — self-deletes when empty (Redis auto-removes empty ZSETs) |
| `{ns}:domain:{d}:next_time` | STRING | Overwritten on every claim from that domain | No — one key per domain, not per URL |
| `{ns}:domain_heads` | ZSET | Resynced on every domain-queue mutation | No — bounded by distinct domain count |
| `{ns}:domains:active` | SET | Mirrors `domain_heads` | No — bounded by distinct domain count |
| `{ns}:inflight` | ZSET | Added on claim, removed on completion/reclaim | No — bounded by concurrently-claimed URLs |
| `{ns}:claim:{url}` | HASH | Created on claim, deleted on completion/reclaim | No — one per currently-inflight URL only |
| `{ns}:attempts:{url}` | STRING | Alive only while URL is non-terminal | No — deleted at every terminal transition |
| `{ns}:retry_scheduled` | ZSET | Added by `mark_failed`/reclaim, removed on promotion | No — bounded by currently-retrying URLs (assuming §4.9 doesn't apply) |
| `{ns}:meta:{url}` | HASH | Created at `add_url`, deleted/TTL'd at terminal transition (`terminal_meta_ttl_seconds`, default 0 = immediate) | No, with the default. **Yes** if an operator sets a long `terminal_meta_ttl_seconds` for retention — that's an explicit, documented tradeoff (ADR §9), not a bug. |

**Estimate at scale.** Per URL that reaches a terminal state, the permanent
keyspace cost is: one membership in `urls:known` + one membership in exactly
one terminal SET. A Redis set member's incremental cost is roughly the string
length of the URL plus small fixed overhead (SET entries in Redis are compact;
for a `listpack`/hashtable-encoded set, figure roughly 50-100 bytes per member
for typical URL lengths, plus quantized allocator overhead).

- **10K URLs:** ~2-3 MB steady state — noise.
- **100K URLs:** ~20-30 MB steady state — still noise relative to typical
  Redis deployment sizing.
- **1M URLs:** on the order of 150-300 MB steady state (2 set memberships/URL
  × ~1M × ~100-150 bytes, generously). This is a real but entirely manageable
  number for any Redis instance provisioned for a distributed crawl, and
  matches the ADR's own framing — nothing here contradicts that design intent.

**The actual unbounded-growth risk in this system is not in Redis at all** — it
is `datasets/domain_blacklist.txt` (§4.4), a local file that grows forever via
`add_to_blacklist()`/`should_auto_blacklist()` with no pruning, deduplication
pass, or size cap, and whose growth directly *worsens* a real, measured
performance bug rather than being merely a passive memory cost. This is the
one part of the "Redis data model" objective whose answer is "look outside
Redis."

One more local-frontier-only note: `URLFrontier._url_to_query`
(`core/url_frontier.py:48`) is never pruned for any URL, terminal or not —
unlike Redis's `meta:{url}`, which is deleted at terminal transition. This is
a real in-process dict that grows for the life of a run, proportional to every
URL ever added with a non-empty `source_query`. For a bounded (`--max-pages`)
run this is irrelevant; for `--indefinite-run` (which exists as a CLI flag,
`main.py:34-38`) it is a slow, real memory leak, though almost certainly
smaller in practice than a same-scale Redis deployment's overhead and bounded
by a single process's URL-processing volume rather than a distributed
namespace's.

---

## 6. Claim / Lease / Recovery Edge Cases Beyond the Existing Tests

The Step 6 crash-recovery and heartbeat scenarios pass and are correctly
implemented. These are edge cases the existing suite does not exercise:

1. **Reclaim-to-requeue takes at least two recovery sweeps, not one.**
   `reclaim_and_promote`'s phase (a) moves an expired inflight claim into
   `retry_scheduled` with `not_before = now + backoff`; phase (b) of the
   *same* invocation only promotes entries already due (`ZRANGEBYSCORE -inf
   now`). A freshly-reclaimed entry's score is in the future, so it cannot be
   promoted until a *later* sweep. Confirmed directly in
   `benchmark/results/crash-recovery/crash_lttl5_bbo1_test.json`'s own
   timeline: `recovery_sweep_1` reclaims (0 requeued), `recovery_sweep_2`
   (1.2s later) requeues. Total time-to-reclaimable is therefore
   `lease_ttl + base_backoff·2^0 + up to one extra recovery_interval`, not
   `lease_ttl + recovery_interval` as informally stated in
   `frontier-step6`-adjacent docs' soak-test framing. Not a bug — just worth
   documenting precisely, since it affects how operators should reason about
   worst-case recovery latency.
2. **A transient `RedisError` during a heartbeat's `renew_claim` call
   propagates as a raw exception**, not a clean `ClaimLostError` (already
   flagged as a known limitation in `frontier-step5.md`, confirmed still true
   by reading `core/claim_heartbeat.py:115-159` — the `except BaseException`
   in `run_with_heartbeat` does catch and clean up, but the worker's outer
   `except Exception` then calls `mark_failed`, consuming a real retry
   attempt for what may have been a perfectly healthy, still-succeeding fetch
   interrupted only by a Redis blip).
3. **Mass-crash drain time is bounded by `reclaim_batch_size`, not by how many
   claims actually expired.** `reclaim_and_promote(batch_size)` processes at
   most `batch_size` (default 200) expired entries per sweep
   (`LIMIT 0 batch_size` in the Lua script). If an entire worker fleet dies
   simultaneously (e.g. an OOM event), recovering, say, 10,000 abandoned
   claims at the default `recovery_interval=30s` takes on the order of
   `10000/200 × 30s ≈ 25 minutes`. Not incorrect — every claim is eventually
   recovered — but a real, plausible operational latency that no existing
   test (all use small claim counts) would surface.
4. **`recovery_enabled=False` for a Redis frontier** — covered in §4.9,
   included here because it's fundamentally a recovery-lifecycle gap.

No data-loss, double-completion, or claim-corruption scenario was found beyond
what's already covered by the existing test suite — the token-validation
design (§3) genuinely closes those.

---

## 7. Redis Failure Behavior — Can URLs Be Lost, Mis-marked, or Stuck?

Answered directly by reading every `except redis.RedisError` branch in
`core/redis_frontier.py`:

| Operation | On Redis error | Consequence |
|---|---|---|
| `add_url` | Returns `False`, logs | **URLs silently lost** (§4.2) — no fallback, no retry |
| `get_next_url` | Returns `None`, logs | Safe — scheduler just polls again; no work was claimed |
| `renew_claim` | Returns `None`, logs | Treated as `ClaimLostError`; in-flight fetch aborted, claim abandoned, retried later via lease expiry — wasted work, not lost work |
| `mark_visited`/`mark_failed`/`mark_skipped` | Logs, returns `"error"` (never checked by callers) | **Completion silently lost** (§4.3) — URL stays inflight, gets re-crawled later; crawler's own counters are already wrong by the time this happens |
| `has_pending`/`get_status_counts`/`pending_count` | Returns `False`/all-zero, logs | **Can trigger premature full-crawler shutdown** (§4.1) |
| `reclaim_and_promote` | Returns `(0, 0)`, logs; outer loop in `crawler_manager.py:310-319` also catches generically | Safe — sweep just no-ops until Redis recovers |
| Worker cancelled mid-Redis-call (`asyncio.to_thread`) | The OS thread running the blocking `redis-py` call **cannot be forcibly killed** by cancelling the awaiting coroutine — it runs to completion in the background regardless | Usually harmless (the Redis op still applies correctly server-side) but means a "cancelled" operation isn't actually stopped, and if `frontier.close()` runs concurrently during shutdown (`crawler_manager.py:349-350`, a direct synchronous call after `await asyncio.gather(...)`), there's a narrow window where an orphaned thread could still be using the connection pool being closed. Not observed to cause data corruption (Redis operations are atomic server-side regardless of when the client-side result is read), but could produce a spurious, unlogged exception in a daemon thread with no one listening for it. |

**Verdict:** URLs can be silently lost (add_url path) and completions can be
silently dropped leading to wasted re-crawls (mark_* path); URLs are not
duplicated or incorrectly marked as terminal by any Redis-failure path found.
The most severe consequence is the premature-shutdown risk in §4.1, since it
can end an entire crawl early rather than just losing/delaying individual
URLs.

---

## 8. Suspected Bottlenecks Requiring Further Measurement

These are plausible, reasoned from the code, but were not directly measured in
this audit (per the "no large benchmark campaign" instruction) — each entry
says exactly what a small, targeted follow-up measurement would look like.

### 8.1 — Production single-process claim throughput is architecturally serial, and no benchmark tested it

`scheduler()` in every crawler backend (e.g. `crawler/async_crawler.py:238-261`)
issues `await self.frontier.get_next_url()` **one at a time, strictly
sequentially** — there is exactly one scheduler coroutine per crawler process,
so a single process's claim rate is capped by
`1 / (single-get_next_url-round-trip-time)`, regardless of `concurrency`
(worker count). Neither Step 6 benchmark measured this: `frontier_benchmark.py`
uses N independent OS *threads* each polling `get_next_url()` concurrently
(testing frontier contention under concurrent claiming, which production never
does), and `distributed_benchmark.py` uses N independent OS *processes*
(testing cross-process coordination, not the single-scheduler-per-process
shape). **What to measure:** instrument (or temporarily log) the real
`AsyncCrawler.scheduler()` loop's claim rate under a synthetic zero-latency
`fetch()` stub, to see the actual per-process claim ceiling once §4.4/§4.5 are
fixed. Expected outcome given the raw Lua round-trip measurement in §1
(~0.08ms): several thousand claims/sec per process, i.e. not a bottleneck at
all for any realistic worker count — but this should be confirmed rather than
assumed.

### 8.2 — `asyncio.to_thread`'s shared thread pool may queue under default `concurrency=25`

`CrawlerConfig.concurrency` defaults to 25 (`core/config.py:105`).
`asyncio.to_thread` reuses the event loop's default `ThreadPoolExecutor`,
capped at `min(32, os.cpu_count() + 4)` — 16 on the benchmark machine used for
this audit (`nproc` = 12). Every worker's `mark_visited`/`mark_failed`/
`add_url` (one call per extracted link) call competes for this pool. With
`concurrency=25 > 16`, it's plausible for completions/adds to queue for a
thread-pool slot under sustained load, independent of Redis's own latency.
Neither benchmark measured this (both bypass `asyncio.to_thread` entirely —
one uses raw threads, the other raw processes). **What to measure:** run the
real `AsyncCrawler` against a zero-latency stub HTTP server with
`concurrency=25`+ and count how many `asyncio.to_thread` calls are queued
(not yet running) at any instant, e.g. via `ThreadPoolExecutor._work_queue`
size sampling or a wrapping semaphore-with-logging around `AsyncFrontier._run`.

### 8.3 — Domain starvation at >`domain_scan_limit` domains (§4.6) — needs a direct reproduction

**What to measure:** seed ≥100 distinct domains with continuously-replenished
queues (e.g. via `priority_ratelimit.py`'s scenario mechanism, extended) at
varying priorities, run `get_next_url()` in a loop, and check whether any
domain ranked outside the top `domain_scan_limit` is ever actually claimed
within a bounded time window. This is a direct, cheap reproduction of §4.6's
reasoning that this audit did not run (to respect the "no large benchmark
campaign" instruction), but it's a small, targeted script, not a campaign.

---

## 9. Potential Optimizations, Risk, Expected Benefit, and Justification

| # | Optimization | Risk | Expected benefit | Justified now? |
|---|---|---|---|---|
| 1 | Fix `URLUtils`'s blacklist cache so it doesn't self-invalidate every call (e.g. don't `touch()` an already-existing file; only reload on an actual content change) | Low — narrowly scoped, well-covered by existing blacklist tests, no protocol/keyspace change | Removes ~5.7ms from every `add_url`/`get_next_url`/`get_link_priority` call — this is the dominant cost in every throughput number in the Step 6 evidence. Directly measured 77x gap. | **Yes — highest-value, lowest-risk fix identified in this audit.** |
| 2 | Offload `is_blacklisted()`/`get_link_priority()` calls in `worker()` via `asyncio.to_thread` (or fix #1 so the cost is negligible enough not to need offloading) | Low-medium — touches all 7 crawler backends' `worker()` methods (mechanical, same shape as prior migration steps) | Stops blocking the event loop for the crawl's actual concurrency to matter; directly protects the thing Step 4 was built to protect | **Yes, but likely unnecessary if #1 is fixed** — a sub-0.01ms in-memory set lookup doesn't need thread offload. Do #1 first and re-measure before deciding whether #2 is still needed. |
| 3 | Make `has_pending`/`get_status_counts`/`pending_count` distinguish "genuinely empty" from "Redis is unreachable" (e.g. raise, or return a distinguishable sentinel, rather than `False`/zero) | Low-medium — changes an error-handling contract callers rely on; needs care not to make the scheduler treat every blip as fatal either | Removes the premature-shutdown risk (§4.1), the most severe correctness gap found | **Yes — P0 correctness gap, not a performance question at all.** |
| 4 | Give `add_url`/`mark_*` callers a way to know a completion/insert actually failed (propagate, log at a level that pages, or retry-with-backoff at the call site) | Medium — touches the `Frontier` protocol's error-handling contract and every one of the 7 worker loops | Removes silent URL loss (§4.2) and silent duplicate-work (§4.3) | **Yes for the failure-visibility part (e.g. logging/metrics); the full retry-the-Redis-call mechanism is a larger design question worth its own review, not a quick patch.** |
| 5 | Raise `domain_scan_limit` default, or change `claim_next` to periodically reconsider domains beyond the initial `K` window (e.g. rotate which domains occupy the scanned window) | Medium — this is the one item that touches the Lua claim script, the piece the ADR was most deliberate about keeping O(1)/O(K) | Removes the domain-starvation scenario (§4.6) at real-world domain counts | **Conditionally — confirm via §8.3's targeted measurement first. If reproduced, this needs a real design decision (the ADR's own K-bound tradeoff was deliberate), not a quick tweak.** |
| 6 | Fix `BatchedDatabaseWriter` to actually batch (or rename it and accept it's synchronous-per-write) | Low | Removes redundant per-operation SQLite commits | **P2 — real but secondary to #1/#2; worth doing, not urgent.** |
| 7 | Remove the duplicate `url_database.update_status` call (either the frontier's internal one or the worker's) | Low | Halves SQLite write volume for Redis-backed crawls | **P2 — cheap, safe, low priority.** |
| 8 | Validate `recovery_enabled=False` + `type: redis` at startup (warn or refuse) | Low | Prevents a silent-forever-stuck-crawl footgun | **Yes — cheap guard, worth adding.** |
| 9 | Increase `asyncio.to_thread`'s effective pool size, or move to `redis.asyncio.Redis` | Medium-high — the latter is the larger migration Step 4 explicitly deferred | Addresses §8.2 *if* it's confirmed to matter | **Not yet — unmeasured (§8.2); do the measurement before deciding between "raise the executor's `max_workers`" (cheap) and "migrate to a native async client" (expensive).** |

---

## 10. Code-Quality Issues

Checked against the project's stated rules. Findings only — no fixes applied.

- **Function length.** Every crawler backend's `worker()` (e.g.
  `crawler/async_crawler.py:135-236`, ~100 lines) and `run()` exceed the ≤30
  line guideline substantially. This is consistent across all 7 backends and
  was a deliberate, acknowledged shape from the original ADR migration (a
  single `try/except` block covering blacklist-skip, fetch, parse, and every
  completion path was kept intentionally linear for auditability across the
  claim lifecycle). Worth a look, but breaking it up risks obscuring exactly
  the claim-lifecycle invariants (`mark_*` called exactly once per exit path)
  that the ADR was most careful about — any refactor here should be reviewed
  against `frontier-step2.md`'s explicit enumeration of every exit path.
- **Duplicated logic across 7 files.** `worker()`/`scheduler()`/`run()` are
  near-identical across `async_crawler.py`, `http_crawler.py`, `tor_crawler.py`,
  `playwright_crawler.py`, `selenium_crawler.py`, `scrapling_crawler.py`, and
  `hybrid_crawler.py`, with no shared base class (`frontier-adr.md` §0 notes
  this explicitly: "no common base class — each reimplements it
  independently"). This is a real, acknowledged "no duplicated logic"
  violation, consistently applied rather than accidentally drifted (each
  migration step mechanically re-applied the same edit to all 7 files) — a
  genuine maintainability cost (a bug fix like §4.4/§4.5 needs to be verified
  in 7 places), but not a correctness risk today given the mechanical
  discipline used so far.
- **Dead code.** `core/scheduler.py`'s `Scheduler` class and
  `core/worker_pool.py`'s `WorkerPool` class are both fully unused — confirmed
  via repo-wide grep (`Scheduler(`/`WorkerPool(` appear nowhere outside their
  own definitions, including in `tests/`). Both are kept "for interface
  consistency" per the Step 2 notes, but neither is exercised by any test or
  reachable from any entry point. Candidates for removal or an explicit
  "intentionally unused, kept for X" docstring if there's a forward-looking
  reason to keep them.
- **`except Exception` breadth.** Every crawler backend's `worker()` has one
  broad `except Exception as e: ... mark_failed(...)` catch-all
  (3-6 occurrences per file). This is a deliberate, ADR-documented design
  choice (§0: "today the URL is silently dropped... under the new claim model
  this is exactly the case lease-recovery exists for... it should still fail
  fast"), not an accidental broad catch — it exists specifically so no
  exception type can leak a claim. Technically a "narrow exception handling"
  rule violation, but a reasoned, intentional one; narrowing it further would
  require enumerating every exception type every parser/media-DB/network call
  could raise, which is its own maintenance burden. Low priority.
- **Misleading naming: `BatchedDatabaseWriter`.** Covered in §4.7 — the name
  and `batch_size` parameter promise behavior the implementation doesn't
  deliver. This is as much a naming/documentation problem as a performance
  one: a future maintainer reading `add_url`'s call site would reasonably
  assume writes are batched and cheap, when they're actually a full commit
  each time.
- **Stale documentation.** `REDIS_MULTIWORKER_SUMMARY.md` describes an
  earlier, pre-ADR architecture (references a 0.3s default rate limit — the
  actual default is now 1.0s per `core/config.py:108`; describes "0-latency
  atomic operations"; doesn't mention claims, leases, heartbeat, or recovery
  at all). This document will actively mislead anyone reading it after the
  ADR migration. `docs/DISTRIBUTED_SETUP.md`'s performance table (50-800
  URLs/**hour**) is still accurate in spirit (real crawling is bounded by
  HTTP fetch time and target politeness, not frontier claim speed — see §11),
  but should be read alongside this audit's finding that the frontier itself
  is capable of orders of magnitude more throughput than a real crawl will
  ever demand from it, once §4.4 is fixed.
- **No validation on `recovery_enabled=False` + `type: redis`** — covered in
  §4.9/§9.8, listed here too since it's as much a "no silent misconfiguration"
  code-quality gap as a correctness one.

---

## 11. Observability

Can a production user currently answer the ten questions posed in the audit
brief?

| Question | Answerable today? | How |
|---|---|---|
| How many URLs exist? | Yes | `get_status_counts()` sums to `known` |
| How many are queued? | Yes | `get_status_counts()["queued"]` (derived arithmetically, O(1) — correct by construction as long as the six counted buckets stay exhaustive) |
| How many are inflight? | Yes | `get_status_counts()["inflight"]` (`ZCARD inflight`) |
| How many are retry-scheduled? | Yes | `get_status_counts()["retry_scheduled"]` |
| How many were visited? | Yes | `get_status_counts()["visited"]` |
| How many permanently failed? | Yes | `get_status_counts()["failed_permanent"]` |
| How many are currently claimed? | Yes (same as inflight) | — |
| How many domains are active? | **Partially** — `domains:active` SET exists and is maintained, but **no method on `RedisURLFrontier` exposes its size or contents**; an operator would have to `SCARD crawler:domains:active` directly against Redis, bypassing the `Frontier` API entirely | Gap |
| How many claims are being reclaimed (rate, not just a point-in-time count)? | **No** — `reclaim_and_promote` returns `(reclaimed, requeued)` counts *per call*, logged at `DEBUG` only when non-zero (`crawler_manager.py:313-314`), with no cumulative counter, metric, or way to query "how many reclaims happened in the last hour" after the fact | Gap |
| How many retries are occurring (rate)? | **No** — same issue; `retry_scheduled`'s current size is visible, but the *rate* of failures flowing into it is not tracked anywhere | Gap |

**Verdict:** the six-bucket `get_status_counts()` partition is a genuine,
well-designed improvement over the pre-ADR Redis implementation (which the
task brief notes was "difficult to inspect compared with SQL") for
point-in-time state. What's missing is **rate/event observability**: nothing
in this system counts reclaims, retries, add_url failures (§4.2), or
completion failures (§4.3) over time — every one of those currently only
produces a log line, not a metric. For a system this audit has just shown can
silently drop work under failure conditions (§4.1-4.3), the absence of a
reclaim-rate or failure-rate counter is a real gap: an operator has no way to
notice "Redis has been flaky for the last hour and we've silently dropped N
URLs" without grepping logs.

---

## 12. Recommended Implementation Order

Ordered by (severity × confidence × how much it unblocks correctly diagnosing
everything else):

1. **Fix the `URLUtils` blacklist self-invalidating cache (§4.4).** Highest
   confidence (directly measured, 77x effect), lowest risk, and unblocks
   accurate re-measurement of everything else — right now, every throughput
   number in the Step 6 evidence is dominated by this one bug, so any other
   optimization would be evaluated against a false baseline until this is
   fixed.
2. **Re-run a small subset of the Step 6 benchmarks after #1**, specifically
   `frontier_benchmark.py --frontier redis --workers 1` and `--workers 4`
   with `--no-rate-limit`, to establish the *real* per-backend ceiling before
   deciding whether #2 below (thread-offloading the blacklist check) is still
   needed at all.
3. **Fix `has_pending`/`get_status_counts` Redis-error handling (§4.1).**
   P0 correctness gap, independent of and unaffected by #1 — do not wait on
   benchmark re-runs for this one.
4. **Add visibility (at minimum, logging one level up from `DEBUG`, ideally a
   counter) for `add_url` failures, completion failures, and reclaim/retry
   rates (§4.2, §4.3, §11).** Makes every remaining failure-handling gap
   observable in production instead of silent.
5. **Run the targeted domain-starvation reproduction (§8.3).** Decide whether
   §4.6 needs a design change based on real evidence, not the reasoning alone.
6. **Address the worker-loop blocking-I/O gap (§4.5)** if #1's fix doesn't
   make it moot (re-measure first — a fixed cache lookup may be cheap enough
   not to need thread-offloading).
7. **Lower-priority cleanup**, any time: `BatchedDatabaseWriter` (§4.7), the
   duplicate `url_database` write (§4.8), the `recovery_enabled` validation
   guard (§4.9), stale docs (§10), dead code removal (§10).

---

## 13. Things That Should NOT Be Changed

- **The Redis keyspace and Lua script design.** No correctness or scalability
  problem was found in `claim_next`, `complete_claim`, `renew_claim`, or
  `reclaim_and_promote` themselves. The atomicity guarantees are real and
  well-tested. Re-architecting this now, based on benchmark numbers that this
  audit has shown are dominated by an unrelated bug, would be solving the
  wrong problem.
- **The claim/lease/token model.** Genuinely correct, per §3 and §6. Don't
  touch it to chase throughput.
- **The decision not to migrate to `redis.asyncio.Redis`.** Step 4's own
  reasoning for staying with `asyncio.to_thread` (avoid a large protocol
  migration until there's a concrete reason) remains sound — §8.2 identifies
  a *plausible* reason but it is unmeasured; don't take on that migration's
  risk until it's confirmed necessary, and even then, consider first the much
  cheaper option of raising the default executor's `max_workers`.
  raising the shared executor's `max_workers`.
- **The `domain_scan_limit` / `K`-bounded scan mechanism itself.** The bound
  exists for a real reason (bounded worst-case Lua execution time) and the
  ADR was explicit and deliberate about this tradeoff. §4.6/§9's
  recommendation is to *measure* the starvation scenario, not to remove the
  bound.
- **The heartbeat/renewal design (`core/claim_heartbeat.py`).** Already
  reviewed and hardened across Step 5; this audit found nothing new here
  beyond the already-self-documented `RedisError`-during-renewal limitation
  (§6.2), which is a known, accepted tradeoff, not a fresh problem.
- **The six-bucket `get_status_counts()` state machine.** It's correct and
  exhaustive; the observability gap (§11) is about *rates*, not about this
  point-in-time API being wrong.
- **The local (`URLFrontier`) frontier's synchronous, lease-free design.**
  Correct by the ADR's own reasoning (no crash-recovery scenario exists
  in-process) — the `_url_to_query` unbounded-growth note in §5 is worth
  knowing about for `--indefinite-run`, not a reason to add Redis-style lease
  machinery to a single-process frontier.

---

## Concise Summary

**CONFIRMED PROBLEMS**
- P0 — `has_pending()`/`get_status_counts()`/`pending_count()` return "nothing
  pending" on any Redis error → can trigger premature full-crawler shutdown
  during a transient Redis outage (§4.1, §7).
- P0 — `URLUtils`'s blacklist cache self-invalidates on every call
  (`Path.touch()` bumps mtime, defeating the mtime-based cache check), forcing
  a full file re-parse (currently 1,463 lines) on every `add_url`/
  `get_next_url`/`get_link_priority` call, for both frontier backends.
  Measured: 0.08ms raw Redis round trip vs. 6.07ms full call — 77x. This is
  the dominant cost behind essentially every throughput/latency number in the
  Step 6 evidence (§1, §2, §4.4).
- P0 — that same now-expensive blacklist check runs synchronously, unoffloaded,
  directly on the event loop thread inside every crawler backend's `worker()`
  (once per claim, once per extracted link) — a real gap in Step 4's async-
  correctness coverage (§4.5).
- P1 — `add_url()` silently drops discovered links on any Redis error, with no
  fallback or retry (§4.2, §7).
- P1 — `mark_visited`/`mark_failed`/`mark_skipped` failures are logged and
  swallowed; no caller checks the return value; leads to silent duplicate
  re-crawls and crawler-vs-frontier progress mismatch (§4.3, §7).
- P1 — `domain_scan_limit` is a hard visibility cutoff (not just a rate-gate
  skip list): domains ranked outside the top K by priority are never
  considered for claiming, a real starvation mechanism at domain counts this
  crawler is likely to reach (§4.6).
- P2 — `BatchedDatabaseWriter` doesn't batch; commits on every write despite
  its name and unused `batch_size` parameter (§4.7).
- P2 — Redis-backed completions write to `url_database` twice per completion;
  local frontier writes once (§4.8).
- P2 — `recovery_enabled=False` + `type: redis` silently breaks all retry/
  reclaim forever with no validation warning (§4.9).

**SUSPECTED PROBLEMS (need the targeted measurements in §8, not a rewrite)**
- Whether `asyncio.to_thread`'s shared, capped thread pool queues under the
  default `concurrency=25` in the real production path (§8.2) — neither
  benchmark tested this execution path at all.
- Whether domain starvation (§4.6) is reachable at this crawler's real domain
  counts — reasoned but not directly reproduced (§8.3).
- Whether the single-scheduler-per-process serial claim design (§8.1) is ever
  actually a limiting factor for real crawling (very likely not, given the
  0.08ms raw Redis latency, but unconfirmed against the real `AsyncCrawler`
  code path).

**NO PROBLEM / KEEP AS-IS**
- Redis keyspace design, Lua script atomicity, claim-safety, lease/recovery
  correctness, heartbeat design, `get_status_counts()`'s six-bucket state
  machine, the decision to stay on `asyncio.to_thread` instead of migrating to
  `redis.asyncio.Redis`, Redis memory growth at 10K-1M URLs (§5).

**RECOMMENDED CHANGES (in order — see §12 for full reasoning)**
1. Fix the blacklist cache (§4.4) — highest confidence, lowest risk, unblocks
   accurate re-measurement of everything else.
2. Re-measure claim/insert throughput after #1 before deciding anything else.
3. Fix Redis-error handling in `has_pending`/`get_status_counts` (§4.1) —
   independent of #1, do not wait.
4. Add failure-rate observability for add_url/completion/reclaim (§4.2, §4.3, §11).
5. Run the domain-starvation reproduction (§8.3) and decide on §4.6 from real
   evidence.
6. Re-evaluate whether the worker-loop blocking-I/O gap (§4.5) still needs
   fixing after #1.
7. Low-priority cleanup: `BatchedDatabaseWriter` naming/behavior (§4.7),
   duplicate SQLite write (§4.8), config validation guard (§4.9), stale docs,
   dead code.

**DO NOT CHANGE**
- The Redis keyspace, Lua scripts, claim/lease/token model, `domain_scan_limit`
  mechanism itself (measure first), heartbeat/renewal design, the
  `asyncio.to_thread` execution-boundary decision, and the local frontier's
  synchronous design.
