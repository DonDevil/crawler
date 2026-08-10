# Redis vs SQLite — Architecture Boundary Decision (Audit Only)

Status: **audit / design only — no code changed.** `git status` at the time
of writing shows no changes to any `core/*.py`, `crawler/*.py`, or
`storage/*.py` file from this task. This document answers one question:
what is the correct long-term role of Redis vs. SQLite, given that Redis was
always intended as the production distributed backend and SQLite as an
independent standalone/development backend, never a permanent Redis
dependency.

Sources read in full before writing this: `docs/architecture/audit.md`,
`frontier-adr.md`, `frontier-redis-failure-semantics.md`,
`domain-starvation-audit.md`, `sql-persistence-audit.md`,
`throughput-ceiling-audit.md`, `REDIS_MULTIWORKER_SUMMARY.md`,
`IMPLEMENTATION_STATUS.md`, `catchup.md`; plus direct re-verification of the
current source (`core/crawler_manager.py`, `core/redis_frontier.py`,
`core/frontier_executor.py`, `core/config.py`, `main.py`, `config.yaml`,
`storage/media_evidence_database.py`, and all 7 crawler engines) rather than
trusting the prior audits' line numbers blindly. `frontier-optimization-audit.md`,
`domain-scan-limit-decision.md`, and `domain-scan-window-design.md` were
consulted at the depth `domain-starvation-audit.md`/`throughput-ceiling-audit.md`
already cite them (their K-bound findings are folded in below); they concern
domain-scheduling fairness, not the Redis/SQLite boundary, and are not
re-litigated here.

---

## 1. Traced Redis path — where SQLite currently sits

```
main.py (argparse; no --redis/--sql flags exist — see §1a)
  → CrawlerManager.__init__                            core/crawler_manager.py:69-124
      → URLDatabase(sqlite_path)                        line 69  — UNCONDITIONAL
      → DomainDatabase(sqlite_path)                     line 70  — UNCONDITIONAL
      → MediaEvidenceDatabase(media_sqlite_path)         line 71-75 — gated only on
                                                          config.enable_media_evidence,
                                                          NOT on frontier type
      → frontier_type == "redis":
          RedisURLFrontier(..., url_database=self.url_database)   line 82-95
        else:
          URLFrontier(..., url_database=self.url_database)        line 107-123
  → AsyncFrontier(self.frontier)                        line 132 — offload adapter
  → crawler = AsyncCrawler(..., url_database=.., media_database=..)  line 137-186
      (identically for the other 5 engines)
```

**§1a — CLI reality check.** The task brief frames `--redis`/`--sql` as
existing alternative CLI modes. They do not exist as flags today (verified:
`main.py`'s only `argparse` surface is seed/query/engine/page/debug/
clear-db/blacklist/fingerprinter-CLI flags — grep confirms no `--redis` or
`--sql`). Backend selection is `config.yaml: crawler.frontier.type`
(`"redis"` or `"sqlite"`), currently defaulting to `"redis"`
(`config.yaml:15`). This doesn't change the substance of the audit, but the
migration plan (§11) should decide whether to introduce explicit
`--redis`/`--sql` flags as part of formalizing the boundary, since
config-only selection makes it easy for a `--sql` "standalone dev mode" and
a `--redis` "production mode" to silently diverge in ways a flag would make
explicit at the CLI surface.

**§1b — SQLite construction is unconditional, confirmed current.** Every
`CrawlerManager` instance opens `crawl_state.db` and (if media evidence is
enabled, which it is by default) `media_evidence.db`, regardless of
`frontier.type`. This is not vestigial: `url_database` is threaded into
`RedisURLFrontier`'s constructor and called from inside three of its four
mutating methods.

**§1c — Redis operation → SQLite operation call graph (per URL, Redis mode).**
Traced through `AsyncCrawler` (the default `engine: "async"`); all 6 other
engines (`http`, `hybrid`, `playwright`, `selenium`, `scrapling`, `tor`)
replicate the identical sequence at the same points (confirmed by grep across
all 7 files, not assumed):

```
scheduler():
  RedisURLFrontier.get_next_url()                 core/redis_frontier.py:462
    → Lua EVALSHA (1 Redis RTT, atomic claim)
    → url_database.update_status(url,"pending")    line 520-521  [SQLite write #1]

worker():
  url_database.add_url(url, status="pending")      crawler/async_crawler.py:159
                                                      [SQLite write #2 — duplicate of #1]
  fetch():
    (raw media byte-stream short-circuit) → media_database.record_media_link()
                                              (3 stmts, no explicit guard in fetch())
  parse HTML:
    for each media link:  media_database.record_media_link()   [try/except-wrapped]
    for each discovered link:
      AsyncFrontier.add_url(link) → RedisURLFrontier.add_url()
        → Lua EVALSHA (1 Redis RTT, atomic dedup+enqueue)
        → url_database.add_url(cleaned,"queued")   line 457-458  [SQLite write #3, ×N —
                                                      NO per-link try/except, see §1e]
  completion:
    RedisURLFrontier._complete()                    line 568-609
      → Lua EVALSHA (1 Redis RTT, atomic completion)
      → url_database.update_status(claim.url, db_status)  line 600-607  [SQLite write #4]
    url_database.update_status(url, status)         crawler/async_crawler.py:210
                                                      [SQLite write #5 — duplicate/
                                                       conflicting with #4, see §1d]
```

Net: **4 + N SQLite statements minimum per successful URL with N discovered
links**, plus 3 more per media link (all confirmed still present in the
current tree, not a stale finding from the earlier `sql-persistence-audit.md`
— re-verified line-for-line above).

**§1d — Confirmed still live: the retry-status clobber bug.**
`RedisURLFrontier._complete()` correctly writes `db_status="queued"` when
Redis reports `retry_scheduled` (line 604). `AsyncCrawler.worker()`'s own
follow-up write at line 210 only ever writes `"visited"` or `"failed"` (set
at line 167/201) — it has no concept of `retry_scheduled` — so it
immediately overwrites the correct `"queued"` back to `"failed"`. A URL
Redis is still actively retrying reads as permanently failed in the SQLite
mirror, corrupting `--unfinished` resume specifically for the
retry-in-backoff case. Confirmed present in `async_crawler.py:200-210` and,
by the same grep-verified pattern, in all 6 other engines.

**§1e — Confirmed still live: the uncaught-mirror-write-drops-links bug.**
The discovered-links loop (`async_crawler.py:198-199`) has no per-link
`try`/`except`. `RedisURLFrontier.add_url`'s SQLite mirror write (line 458)
is not wrapped either. A `sqlite3.OperationalError` on link 3 of 20 aborts
the loop — links 4-20 are never even offered to Redis, and the page's own
claim gets marked failed by the outer handler purely from a local-disk
fault. Redis's own state is untouched by this (link 1-2 already succeeded
there), so this is a pure SQLite-caused, Redis-side-success-turned-into-a
reported failure. This is the sharpest concrete argument for removing
SQLite from the hot path entirely rather than hardening it in place.

**§1f — One correction to the older `sql-persistence-audit.md`'s framing.**
That audit's §11 states "no `url_database`/`media_database` call is
offloaded to a thread." That's still true for the **worker-side duplicate
calls** (`async_crawler.py:155,159,210` and the equivalent lines in the
other 5 engines — these run directly on the event-loop thread, confirmed
above). But it is *incomplete* for the **three mirror writes embedded inside
`RedisURLFrontier` itself** (§1c's writes #1, #3, #4): when reached through
`self.frontier` (the `AsyncFrontier`-wrapped object), the entire synchronous
`get_next_url`/`add_url`/`_complete` method — Redis Lua call *and* its
trailing SQLite write together — is offloaded as one unit via
`asyncio.to_thread` (`core/frontier_executor.py:58-73`, confirmed:
`AsyncFrontier._offload = not isinstance(frontier, URLFrontier)`, true for
Redis). So those three writes don't block the event loop; they consume a
slot in the shared default thread pool for their duration instead — still a
resource cost (and the mechanism that makes §1e's uncaught exception surface
as an `await self.frontier.add_url(...)` failure rather than a hard crash),
just not an event-loop stall. The worker's own **duplicate** calls (§1d's
write #2 and #5) get no such benefit — they're plain synchronous calls the
worker makes directly against `self.url_database`, never through
`self.frontier`/`AsyncFrontier` at all.

---

## 2. Clean backend boundary — what's actually shared vs. backend-specific

| Component | Redis mode needs it? | SQL mode needs it? | Verdict |
|---|---|---|---|
| `RedisURLFrontier` (frontier logic, dedup, priority, claims, leases, retries) | Yes — is the authority | No | Redis-only |
| `URLFrontier` (in-memory heap + SQLite mirror) | No | Yes — is the authority | SQL-only |
| `URLDatabase` (`urls`/`domains` tables) | **No, not for scheduling** — currently used only as (a) a best-effort resume mirror for `--unfinished` after *total* Redis loss, and (b) the startup-seeding durable-defer target (§6/§7) | **Yes** — `URLFrontier` uses it for crash-recovery dedup (`is_visited`) and IS its resume mechanism | Shared today by construction, not by necessity. Redis mode's only genuine remaining need for it is the narrow startup-seeding defer path (§7) — not the per-URL claim/complete hot path. |
| `DomainDatabase` (`domains.score`) | No — 0 rows in the live db (`sql-persistence-audit.md` §3, re-confirmed: only written by the not-yet-exercised `--mark-match` CLI path) | Same — it's dormant in both modes today | Neither mode needs it on the hot path; it's fingerprinter-feedback plumbing, orthogonal to frontier choice. |
| `MediaEvidenceDatabase` (`media_assets`/`media_observations`/`sample_jobs`/`manifest_variants`) | **Currently yes — it's the only place discovered media is recorded, in both modes.** This is real, authoritative production output, not a mirror. | Yes, same | Shared **today** because no Redis-backed alternative exists yet (§5). This is the one piece of "SQLite in Redis mode" that is not a redundant mirror and cannot simply be deleted from the hot path without a replacement. |
| 6 crawler engines' own direct `url_database`/`media_database` calls (§1c writes #2, #5) | **No** — these are pure duplicates of what `RedisURLFrontier` already writes (status) or genuinely-needed-but-misplaced (media) | Yes for media; the status duplicates matter less here since `URLFrontier` has no separate mirror-writing party to duplicate against | Redis-only-mode should not carry these at all for status (§1d); media writes need to move to whatever replaces `MediaEvidenceDatabase` in Redis mode (§5), not be deleted. |
| `crawl_state_db.py` (`CrawlStateDB`) | No | No | Confirmed dead code both modes (zero callers, table doesn't exist in the live db) — not part of either boundary. |
| `result_exporter.py` | No | No | Confirmed dead code, zero callers. |

**Direct answers to the brief's specific questions:**
- **Is `URLDatabase` required at all in Redis mode?** No, not for anything on the per-URL claim/complete hot path — Redis is fully self-sufficient for scheduling, dedup, priority, and retry. Its only legitimate remaining Redis-mode use is the bounded, one-shot startup-seeding durable-defer mechanism (§7), which is a much smaller surface than "mirror every URL."
- **Is `DomainDatabase` required in Redis mode?** No — it's inert in both modes today (0 rows), gated behind a CLI flag nothing currently calls in the live crawl loop.
- **Is `MediaEvidenceDatabase` required in Redis mode?** Yes, **today**, because it's the only implementation that exists — but it is not required to be *SQLite specifically*; it's required to be *some* durable evidence store, and per the intended architecture that should be Redis-backed (or Redis-fronted) once built (§5).
- **Does any crawler functionality assume SQLite exists?** Yes, structurally: all 7 engines take `url_database`/`media_database` as required-shaped (not `Optional`-checked consistently — `if self.url_database:` guards exist, but `CrawlerManager` always constructs and passes them, so in practice they're never absent in the current wiring) constructor args and call them unconditionally on the hot path. Nothing *breaks* if you pass `None` for `url_database` (the `if self.url_database:` guards would skip the status-mirror writes cleanly), but `media_database=None` would silently stop recording all discovered media — not safe to just null out without a replacement.
- **Does resume/reporting need redesign for Redis mode?** Yes — `--unfinished` currently reads only from SQLite (`load_unfinished_urls`, `core/crawler_manager.py:308-324`) regardless of frontier type; there is **no Redis-side resume path** (confirmed: also flagged by the original `audit.md` §9's CLI table). In a Redis-only production design, "resume" should mean "the frontier already durably holds this state in Redis" (true today — Redis is authoritative and survives a crawler-process restart on its own) rather than "reload from a local SQLite file," except for the disaster-recovery case of *total* Redis data loss, where SQLite's resume role is intentionally last-resort (§6).
- **Does any current Redis functionality accidentally depend on SQLite?** Yes, precisely the correctness bug in §1e: a Redis-mode success (2 of 20 links accepted by Redis) can be turned into a reported page failure by an unrelated local SQLite fault, because the mirror write isn't isolated from the path that's supposed to be primary.

---

## 3. SQL mode — can it remain a valid, minimal-change standalone backend?

**Yes, essentially as-is.** `URLFrontier` (`core/url_frontier.py`) is a
self-contained, pure in-process priority heap with `URLDatabase` as its
genuine, load-bearing persistence layer (not a mirror of anything else in
this mode — it *is* the authority for crash-recovery dedup). `frontier-adr.md`
§10 already documents exactly this: local-frontier behavior is "preserved
exactly," with lease/token bookkeeping intentionally simplified since a
single-process crawl has no cross-process crash-recovery scenario to guard
against. Nothing in this audit found SQL-mode-specific correctness problems —
every issue found in §1 is specifically about SQLite being *dragged into*
Redis mode, not about SQL mode's own behavior.

The one thing SQL mode should **stop doing** under a clean boundary: sharing
`URLDatabase`/`MediaEvidenceDatabase` construction code with Redis mode in a
way that makes it look like Redis mode needs them too (§2's construction
site, `core/crawler_manager.py:69-75`). This is a wiring/clarity issue, not
a SQL-mode functionality gap.

---

## 4. Redis mode — what exists vs. what's missing from the target design

Target (per the brief):

```
--redis
   ├── Redis frontier: dedup, priority, domain scheduling, rate limiting, claims, leases, retries
   └── Redis-backed media evidence/work state: assets, observations, fingerprint jobs, manifests, results/status
```

| Piece | Status |
|---|---|
| Redis frontier — dedup | **Done.** `urls:known` SET, monotonic, `frontier-adr.md` §5. |
| Redis frontier — priority | **Done.** Global `domain_heads` ZSET + per-domain queues, `(priority, seq)` scoring. Strict-priority policy, no fairness beyond rate-gate-skip — confirmed intentional and correctly implemented by `domain-starvation-audit.md` (§1.1-§1.4 there), not a gap. |
| Redis frontier — domain scheduling | **Done**, with one known, already-analyzed tradeoff: `domain_scan_limit` (K, now 250) bounds Lua worst-case runtime and makes domains ranked outside the top K invisible under continuous replenishment of ≥K better-ranked domains (`domain-starvation-audit.md` §4.4/§6). This is a scheduling-fairness question, not a Redis/SQLite boundary question — already flagged for its own decision, not re-litigated here. |
| Redis frontier — rate limiting | **Done.** Single global `rate_limit`, per-domain `next_time` gate. No per-domain override exists (not required by the brief here). |
| Redis frontier — claims/leases | **Done.** `FrontierClaim` token CAS, `inflight` ZSET with lease expiry, `renew_claim`/heartbeat (`claim_heartbeat.py`) wired into all 7 engines. |
| Redis frontier — retries | **Done.** `retry_scheduled` ZSET with exponential backoff, `reclaim_and_promote` background sweep (`crawler_manager._recovery_loop`). |
| **Redis-backed media evidence/work state** | **Missing entirely.** No Redis structures exist for media assets, observations, sample jobs, or manifests anywhere in the codebase or docs. `MediaEvidenceDatabase` is 100% SQLite, 100% local-per-machine. This is the one real gap against the target architecture — see §5. |

The frontier half of the target is essentially complete and well-tested
(`redis_frontier_test.py`, `frontier_redis_failure_semantics_test.py`,
`crawler_manager_seed_failure_semantics_test.py`, `domain_starvation.py`
benchmark — 151+ passing tests across these areas per
`frontier-redis-failure-semantics.md` §7/§11.7). The evidence half doesn't
exist yet.

---

## 5. Media evidence — current implementation, traced

**What's stored** (`storage/media_evidence_database.py`, 4 tables,
confirmed against current schema):
- `media_assets` — one row per discovered media URL (upsert), `status`
  lifecycle `queued_for_sampling → claimed → sampled/hashed/matched/...`.
- `media_observations` — append-only log, one row per (re)discovery event.
- `sample_jobs` — work queue for the not-yet-built fingerprinter, one row
  per asset, `pending → claimed → sampled/hashed/matched`.
- `manifest_variants` — HLS/DASH variant URLs per asset.

**Where:** `storage/media_evidence.db`, local file, path resolved relative
to each process's own base directory (`core/config.py:157-160`) — no
network/shared-filesystem path exists anywhere in config or code.

**Who writes:** every crawler engine's `record_media_link()` call
(triggered from two places per engine: a raw non-HTML response's
content-type sniff inside `fetch()`, and the parsed-HTML media-link loop in
`worker()`) plus `record_manifest_variants()` for HLS/DASH. All synchronous,
inline, on the event-loop thread, in both Redis and SQL mode identically —
media evidence has no frontier-type branching at all today.

**Who reads:** only the CLI side-channel — `main.py --claim-sample-job` →
`claim_next_sample_job()`, `--mark-match` → `mark_asset_matched()`. This
bypasses `CrawlerManager` entirely and talks straight to
`MediaEvidenceDatabase`, on whatever machine's local file happens to be
running the CLI command. **This is the entire current fingerprinter
integration surface** — real, working, and worth preserving behaviorally
(not necessarily the SQLite storage under it) when redesigning for Redis.

**How sample jobs are represented:** a `sample_jobs` row per asset,
`status`/`priority`/`retry_count`/`claimed_by`, claimed via
`claim_next_sample_job(worker_name)` (materializes the full pending set into
Python and takes `[0]` instead of `LIMIT 1` in SQL — a known, low-priority
inefficiency, `sql-persistence-audit.md` §12, irrelevant to this audit's
scope).

**How fingerprint results are represented:** `complete_sample_job()` /
`mark_asset_matched()` update `sample_jobs.status` and
`media_assets.{status, match_confidence, matched_title}`, and optionally
bump `DomainDatabase.score` for the source domain.

**What depends on SQLite:** everything — there is no abstraction boundary
between "media evidence storage" and "SQLite" today; `MediaEvidenceDatabase`
*is* the interface.

**What would need to move to Redis, and why it's a bigger lift than the
frontier was:** the frontier's Redis redesign (`frontier-adr.md`) had the
luxury of an existing, working `URLFrontier` behavioral reference to port
faithfully. Media evidence has no such reference to port — it needs new
design work for:
- A durable work queue (`sample_jobs` semantics — claim/complete/retry) that
  survives a Redis restart, which for the frontier is an accepted risk
  (§6) but for evidence data is not (§2's finding: media evidence is real,
  non-mirrored output, not disposable frontier bookkeeping).
- An append-only observation log at potentially high volume across N
  machines.
- Fleet-wide dedup for `media_assets` (today: two machines independently
  discovering the same media URL produce two disjoint, un-deduplicated
  local rows — already a real gap in the current single-SQLite-per-machine
  design, `sql-persistence-audit.md` §10, that a Redis-backed design would
  need to solve rather than inherit).
- A durability story Redis doesn't provide out of the box the way SQLite's
  on-disk file does (`appendonly`/RDB tuning, or accepting that "evidence
  living only in Redis" is a real product risk until it's drained
  somewhere durable — this is explicitly flagged as a decision point in
  §11, not resolved here).

**What must be preserved during any migration:** the `record_media_link`/
`record_manifest_variants`/`claim_next_sample_job`/`complete_sample_job`/
`mark_asset_matched` **method contract** — `tests/fingerprinter_queue_test.py`
already exercises this surface, and `audit.md` §8 independently flagged it
as "the actual, currently-working interface point toward the fingerprinter...
worth preserving verbatim." The migration should target replacing the
storage underneath this contract, not the contract itself.

---

## 6. Redis outage semantics — Option A vs. Option B

**What's actually built today (confirmed from `frontier-redis-failure-semantics.md`
and re-verified against current `core/redis_frontier.py`/`crawler/async_crawler.py`):
Option A, fully implemented for the ongoing crawl loop.**

- Every `RedisURLFrontier` method that can fail raises `FrontierUnavailable`
  instead of returning an ambiguous sentinel (`core/frontier.py`,
  `core/redis_frontier.py` — every `except redis.RedisError` branch,
  confirmed present in `add_url`, `get_next_url`, `renew_claim`, `_complete`,
  `reclaim_and_promote`, `has_pending`, `get_status_counts`, all still
  raising in the current tree).
- `scheduler()`/`worker()` in all 7 engines catch `FrontierUnavailable`
  explicitly: `idle_loops` resets to 0 (never reads an outage as "done"),
  in-flight claims are abandoned without a false completion call, and the
  existing 0.5s poll loop *is* the retry mechanism — no new busy-loop, no
  new backoff layer.
- Claimed work is preserved via the existing lease/reclaim mechanism
  (`reclaim_and_promote`) once Redis returns — nothing needs replaying,
  because nothing was ever moved out of Redis in the first place.
- This was deliberately chosen, not defaulted into: `frontier-redis-failure-semantics.md`
  §9 states outright that a permanently-down Redis "polls forever at
  crawl time, by design," and explicitly declines to add a circuit breaker
  there because the crawl loop is meant to run indefinitely.

**Option B (auto-switch to SQLite, continue crawling, replay later) is not
built, and this audit recommends against building it, for reasons the
existing docs already substantiate even though no doc frames the question
this way explicitly:**

| Concern | Option A (pause/retry/preserve) | Option B (auto-switch + replay) |
|---|---|---|
| Duplicate crawling | None — Redis dedup (`urls:known`) is untouched; nothing runs while Redis is down | Real risk: a second, independent dedup domain (local SQLite, per-machine) starts accumulating URLs Redis has never seen; on replay, cross-machine duplicates are possible if >1 machine fell back simultaneously (`urls`-table has no cross-machine coordination — confirmed §2/`sql-persistence-audit.md` §10) |
| Priority preservation | Exact — priority lives in Redis (`domain_heads`/`meta:{url}`), untouched by an outage | Lossy — `url_database.add_url` only stores `status`, not priority (confirmed, `frontier-redis-failure-semantics.md` §11.8); a URL crawled during the SQLite-fallback window would need its priority re-derived on replay, same limitation already accepted for the narrower startup-seeding defer path |
| Rate limiting | Exact — `next_time` gates live in Redis, resume where they left off | Would need a **second, parallel** rate-limiting implementation during the fallback window (the local `URLFrontier` has its own, independent rate-limit dict) that then has to reconcile with Redis's state on replay — not free |
| Distributed dedup | Preserved — single source of truth never changes | Broken during the outage window across a multi-machine fleet: each machine's fallback SQLite is an independent dedup domain, exactly the "N disjoint SQLite files" problem `sql-persistence-audit.md` §10 already identifies as the single most consequential gap for *media evidence* — Option B would import that same problem into the *frontier* too |
| Claim ownership / lease state | Preserved — `inflight`/`claim:{url}` untouched | Undefined — the local `URLFrontier` has no claim/lease concept shared with Redis's; a URL claimed locally during fallback has no token Redis would recognize on replay |
| Retry state | Preserved | Would need translating local retry/backoff state into Redis's `retry_scheduled` format on replay, or losing it |
| Reconciliation | None needed — nothing diverged | A genuine, non-trivial merge step: which of possibly-many machines' fallback SQLite files replay first, how are cross-machine duplicates from the outage window detected and dropped, what happens if a URL was completed locally *and* independently discovered+queued on another still-healthy machine during the same window |
| Machine crashes during fallback | N/A (nothing local to lose) | A machine that crashes while in SQLite-fallback mode loses whatever it queued locally and hadn't yet replayed — a second, smaller-scale version of exactly the durability question already open for `--unfinished` today |
| Redis recovery | Immediate — crawl resumes from exactly where it was | Requires an explicit, engineered replay step before the fleet is "caught up" again; until that replay runs, Redis's view of the world is stale relative to what machines did while it was down |
| Partial replay (crash mid-replay) | N/A | New failure mode with no design anywhere in this codebase — what happens if a machine crashes after replaying half its fallback backlog into Redis? |

**Recommendation: keep Option A. Do not build Option B.** This isn't a close
call — Option B doesn't just add a fallback, it adds a second, temporarily-
authoritative frontier implementation with its own dedup/priority/rate-limit/
claim semantics that must later be reconciled with the real one, multiplied
by however many machines are in the fleet. Every one of those reconciliation
problems is exactly the class of distributed-systems bug this whole
redesign (`frontier-adr.md`) exists to eliminate from the *primary* path;
Option B would reintroduce an equivalent one on the *secondary* path. Option
A, which is already built and tested (25 dedicated tests,
`frontier-redis-failure-semantics.md` §7/§11.6), correctly trades "the crawl
pauses during an outage" for "there is nothing to reconcile when it's over"
— the right trade for a system whose correctness properties (no duplicate
crawls, accurate priority, safe claims) are worth more than continuous
uptime during a genuine infrastructure failure.

---

## 7. Temporary SQLite outage spool — narrower design, evaluated

**What exists today is narrower than the brief's Option 7 diagram, and
scoped deliberately:**

```
Redis unavailable during STARTUP SEEDING ONLY (one-shot, bounded)
     ↓
bounded retry (3 attempts) + circuit breaker (3 consecutive exhausted)
     ↓
durable defer: url_database.add_url(url, status="queued")
     ↓
Redis returns
     ↓
--unfinished run picks it up via the EXISTING load_unfinished_urls() query
     (no new replay mechanism — reuses the resume path verbatim)
```

This is real, implemented, and tested (`CrawlerManager._make_seed_url_adder`,
`core/crawler_manager.py:222-278`; `crawler_manager_seed_failure_semantics_test.py`,
8 tests). It is **not** a general-purpose outbox: it only covers
`load_seed_urls`/`load_unfinished_urls`/`load_search_query_urls`, which run
once at process start, in bounded volume, before any concurrent crawl work
exists.

**What's explicitly NOT covered: newly discovered URLs during the ongoing
crawl.** The discovered-links loop (`async_crawler.py:198-199`, §1e) has no
equivalent defer-to-SQLite fallback for a Redis-outage-triggered
`FrontierUnavailable`. Today, if `frontier.add_url(link)` raises
`FrontierUnavailable` mid-crawl (Redis genuinely down, not a SQLite fault),
that propagates to the outer `except FrontierUnavailable` handler
(`async_crawler.py:234-239`), which logs and abandons **the whole claim**
(the page currently being processed) — the discovered links extracted from
that page are not persisted anywhere, not to Redis (unavailable) and not to
SQLite (no defer path exists for this loop). They are silently gone, forever,
the moment that page isn't re-crawled.

**Is a general discovered-link outage spool worth building?** This audit's
answer is **no, not as a durable outbox** — but the underlying loss (a
page's freshly-discovered links vanishing during a Redis outage) is a real,
if bounded, gap worth naming even though fixing it isn't recommended:

- Unlike seeding (operator-provided, small, one-shot), discovered links are
  an ongoing, unbounded stream. A durable outbox for them is exactly the
  "durable outbox/reconciliation system requiring its own correctness
  guarantees" the brief warns about — it would need its own replay
  ordering, its own dedup-against-Redis-once-recovered logic, and its own
  bound on how large it's allowed to grow during a long outage, none of
  which exist today and none of which are as simple as the seeding case's
  "reuse `--unfinished`" trick (seeding runs once before the crawl loop
  exists; discovered links arrive continuously *during* it, from N
  concurrent workers, potentially across N machines).
- The loss is naturally bounded and self-healing in a way that makes
  building this not worth its complexity: the *page itself* isn't lost
  (its claim gets reclaimed via lease-expiry once Redis returns, or the
  worker's own `FrontierUnavailable` handling leaves it abandoned for
  reclaim), so if the crawl continues at all after Redis recovers, that
  page is eligible to be **re-fetched and re-parsed**, which naturally
  re-discovers the same links and re-offers them to `add_url` — the same
  self-healing property normal web crawling already relies on for missed
  links generally (`frontier-adr.md` §0 already accepts this class of loss
  for parser/media-DB exceptions).
- Building a spool here would mean maintaining a second, ongoing,
  concurrent-write persistence path competing for the same `URLDatabase`
  connection that the (recommended, §11) reduced Redis-mode SQLite usage is
  trying to get *out* of the hot path — directly working against this
  document's own recommendation.

**Recommendation: do not build a discovered-link outage spool.** Keep
today's asymmetry (seeding gets a bounded defer mechanism; the ongoing crawl
loop relies on lease-reclaim + natural re-discovery on re-fetch) — it's
already the right shape for the two situations' very different
volume/frequency/boundedness characteristics, and extending the seeding
mechanism's pattern to the crawl loop would recreate exactly the class of
"secondary system needing its own correctness guarantees" both this section
and §6 argue against.

---

## 8. Performance impact — separating Redis latency from SQLite latency

**Redis's own ceiling** (`throughput-ceiling-audit.md`, measured against the
frontier in isolation, no SQLite involved — confirmed explicitly:
"SQLite / url_database — not wired into this benchmark at all"):
- ~13.4-13.8K claim+complete operations/sec at 8-16 concurrent workers
  against a single Redis instance, bottlenecked on Redis's own
  single-threaded Lua execution (Redis CPU rises to 94-96% at that
  concurrency; client CPU never saturates).
- **Critically: with any realistic per-URL work in the loop (a 10ms
  artificial delay, standing in for real HTTP fetch/parse time), Redis CPU
  drops to 5.9%.** The frontier's ceiling is not, and will not become, a
  real constraint once actual network fetches are in the loop — this
  crawler will never generate enough claim/complete volume to approach
  Redis's ceiling under realistic operation.

**SQLite's cost in the same hot path** (`sql-persistence-audit.md`,
re-confirmed live in §1 above):
- 4-32+ synchronous `sqlite3` statements per URL (§1c), **every one
  committed individually** — `BatchedDatabaseWriter` calls `_flush()`
  (commit) after every single `execute()` regardless of `batch_size`, by
  explicit design (its own docstring), not a bug.
- Measured: per-row commit costs a median of 8-15µs but a **heavy-tailed
  max of 162ms** on an otherwise-identical operation (WAL-checkpoint/fsync-
  class stall) — switching to one batched commit for 2000 ops measured
  **203x faster** (654ms → 3.2ms).
- Of the writes in §1c, the three embedded in `RedisURLFrontier` itself
  (writes #1/#3/#4) are incidentally thread-offloaded (§1f) and so cost a
  thread-pool slot, not an event-loop stall. The two **duplicate**
  worker-side writes (#2/#5, §1d) and all `media_database.record_media_link`
  calls run directly on the event-loop thread, unoffloaded — at
  `concurrency=25` (config default), one 162ms-tail stall on that thread
  blocks all 25 workers' fetch/parse/claim progress for its duration.

**What removing SQLite from the Redis hot path would eliminate, concretely:**
- The 2 duplicate per-URL status writes (§1d) — 50% of the `urls`-table
  write volume, zero behavior change, and it fixes the retry-status clobber
  bug as a side effect.
- The uncaught-exception failure mode in §1e entirely (a local-disk fault
  can no longer turn a Redis-side success into a reported crawl failure,
  because there'd be nothing local to fault on).
- All remaining event-loop-thread-blocking SQL calls from the frontier's own
  hot path (writes #1/#3/#4 in §1c) — those three would simply not exist in
  a Redis-only mode, freeing the thread-pool slots they currently occupy.
- Media evidence writes would remain (§5 — nothing to replace them with
  yet), so this is not a full elimination of SQLite-caused latency, but it
  removes everything that's pure frontier-mirror redundancy.

**Bottom line:** Redis is not, and will not become, the throughput
bottleneck for this crawler. SQLite-in-the-hot-path is a real, measured,
already-quantified source of both average-case waste (duplicate writes) and
tail-latency risk (unbatched commits, occasional multi-hundred-millisecond
stalls), independent of and additive to whatever cost real HTTP fetching
already dominates. Removing the mirror-write portion of it from Redis mode
is a clean, low-risk win; the media-evidence portion is real work with no
Redis replacement built yet.

---

## 9. Multi-machine architecture — obstacles in current code

Target:

```
N machines × M workers/machine → shared Redis frontier → shared Redis evidence → distributed fingerprinter workers
```

**Frontier half: already matches the target, no obstacles found.**
`REDIS_MULTIWORKER_SUMMARY.md` and `IMPLEMENTATION_STATUS.md` describe and
`core/redis_frontier.py`/`frontier-adr.md` implement exactly this shape —
any number of machines pointing `frontier.redis_host` at one central Redis
instance share dedup, priority, claims, and leases correctly, with the
claim-token CAS design and atomic Lua scripts making concurrent-worker
correctness independent of worker/machine count (confirmed by
`domain-starvation-audit.md` §4.6: 1/2/4/8 concurrent claimers all produced
identical fairness results and zero duplicate claims).

**One documented, load-bearing caveat that's easy to get wrong at fleet
scale:** `domain_scan_limit` (K, currently 250) is a fixed bound, not a
percentage or auto-scaling value. As active-domain count grows — which it
will, with more machines running more workers discovering more domains
simultaneously — a domain ranked outside the top K becomes invisible to
`claim_next` regardless of how long it's waited, until enough
better-ranked domains drain (`domain-starvation-audit.md` §4.4, already
measured against this crawler's real 50-domain seed file at the *old*
K=50 default). `CrawlerManager._sample_domain_scan_telemetry()`
(`core/crawler_manager.py:400-428`) already logs a warning when active
domains exceed K, which is the right operational signal — but nothing
currently *acts* on that signal (no auto-scaling of K, no alerting
integration beyond a log line). Worth a runbook note for fleet operators,
not a code change per this audit's scope.

**Evidence half: the real obstacle, and it's not a bug to fix, it's a
subsystem that doesn't exist yet (§5).** Under the current design, N
crawler machines produce N disjoint, un-deduplicated, un-aggregated
`media_evidence.db` files, with:
- No cross-machine dedup (the same media URL discovered independently on
  two machines becomes two separate `media_assets` rows in two separate
  files).
- No fleet-wide view for the fingerprinter — `--claim-sample-job` only ever
  sees the local machine's `sample_jobs` table.
- No aggregation/sync mechanism anywhere in the codebase (confirmed by grep,
  `sql-persistence-audit.md` §10 — no rsync/S3-upload/replication code, no
  cross-machine query layer).

This is the one genuine multi-machine architectural gap this audit found,
and it's exactly the gap the brief's §5 anticipated: production requires
the evidence store to be fleet-shared, and today it structurally cannot be,
because it's a local SQLite file with no distribution story at all.

**Long-running/multi-day operation:** the `urls`-table `pending`-row
accumulation from ungracefully-terminated runs (`sql-persistence-audit.md`
§13: 80% of the live table's 81,873 rows are stuck `pending`, no TTL/cleanup
exists) is a real risk for `--sql` mode's long-running deployments, but is
**not** a Redis-mode production concern once the hot-path mirror writes are
removed (§11) — Redis mode wouldn't be accumulating that garbage in the
first place, since it wouldn't be writing `urls` rows per-claim at all,
only via the much smaller startup-seeding defer path (§7).

---

## 10. Decision matrix

| Architecture | Correctness | Performance | Distributed | Complexity | Recommendation |
|---|---|---|---|---|---|
| Redis + SQLite mirror (current state) | Medium — retry-status clobber bug (§1d) and uncaught-mirror-write-drops-links bug (§1e) are both live, real correctness defects, not hypothetical | Medium — 4-32+ unbatched SQLite statements/URL adds measured tail-latency risk on top of an already-fast Redis path (§8) | Frontier: good. Evidence: broken (N disjoint, un-aggregated SQLite files, §9) | Medium — two persistence systems on one hot path, duplicated across 7 crawler-engine files | **Do not keep as the permanent shape** — it's the status quo, not a destination |
| Redis only (no SQLite anywhere in Redis-mode process) | High for frontier — but **breaks media evidence entirely** with nothing built to replace it | Best — eliminates all mirror-write cost (§8) | Frontier: good. Evidence: **regresses to nothing** unless built first | Lowest, once evidence storage is solved elsewhere | Correct **end state for the frontier**, but premature to fully cut over for evidence until §5's Redis-backed replacement exists |
| SQL only (`--sql`/`frontier.type: "sqlite"`) | High — behaviorally simple, single-process, no cross-process races to reason about (`frontier-adr.md` §10) | Fine for its scope (single machine, dev/test volumes) | None — by design, not a gap | Lowest | **Correct as the standalone/dev backend**, already in good enough shape (§3) |
| Redis with SQLite fallback (Option B, §6) | **Lower than current** — introduces reconciliation, duplicate-crawl, and priority/rate-limit-loss risks across a multi-machine fleet that don't exist today | Unclear/worse — a second scheduling implementation running during outages, plus a replay cost afterward | Actively harmful — reintroduces per-machine-disjoint dedup, the exact problem Redis was built to solve | High — a durable outbox + reconciliation system, per the brief's own framing | **Reject** (§6) |
| Redis + temporary SQLite outage spool, seeding-scoped only (what's already built, §7) | High — bounded, tested, reuses the existing `--unfinished` resume path with no new replay logic | Negligible cost (one-shot, bounded 4.5s worst case per loader call) | Fine — doesn't affect ongoing cross-machine coordination, only startup | Low — already implemented, already tested (25 tests) | **Keep** — this is not the same thing as Option B and shouldn't be confused with it |

---

## 11. Recommended architecture

```
REDIS  = the production distributed frontier AND (once built) the production
         distributed media-evidence/work-state store. Nothing about Redis
         mode's crawl-time hot path should construct, open, or write to any
         SQLite file except the narrow, bounded, already-built startup-
         seeding durable-defer path (§7), which legitimately needs a durable
         place to record "Redis couldn't take this URL at startup" and
         reuses the existing url_database + --unfinished machinery for it.

SQLITE = the standalone, single-machine development/testing/offline backend
         (--sql / frontier.type: "sqlite"), unchanged from its current,
         already-correct behavior. Also remains, unavoidably for now, the
         storage underneath MediaEvidenceDatabase in BOTH modes until a
         Redis-backed evidence store is designed and built (§5) — this is
         the one deliberate, temporary exception to "Redis mode doesn't
         touch SQLite," not an oversight.
```

This is a boundary clarification, not a rewrite: the frontier half of this
target is already correctly built (§4); what changes is (a) stop writing
duplicate/mirror `urls` rows from the crawl-time hot path in Redis mode, and
(b) design and build a Redis-backed replacement for `MediaEvidenceDatabase`
so Redis mode can eventually stop touching SQLite at all.

---

## 12. Migration plan (staged, not executed)

1. **Remove the SQLite mirror from the Redis crawl-time hot path.**
   - Delete the two duplicate worker-side writes per engine (§1d: the
     `add_url(..., "pending")` and terminal `update_status` calls in all 7
     crawler engines) — fixes the retry-status clobber bug as a side
     effect, zero behavior change for the happy path.
   - Delete the three mirror writes embedded in `RedisURLFrontier` itself
     (§1c writes #1/#3/#4) — `url_database` becomes an optional constructor
     arg `RedisURLFrontier` no longer needs at all in the target end state.
   - This closes §1e's uncaught-exception failure mode as a direct
     consequence (there's no longer a SQLite write in the discovered-links
     path to fault on).
   - Risk: low, mechanical, same pattern across 7 files (matches
     `frontier-adr.md`'s own precedent for how this codebase has handled
     "same fix needed in all 7 backends, no shared base class" changes
     before).

2. **Keep the startup-seeding durable-defer mechanism (§7) exactly as-is** —
   it's the one legitimate remaining Redis-mode use of `url_database`, and
   it's already correctly scoped and tested.

3. **Formalize the CLI surface** (optional but recommended given §1a):
   introduce explicit `--redis`/`--sql` flags (or keep config-only, but
   document the two modes as genuinely separate deployment targets rather
   than two settings of one code path) so the boundary from step 1 is
   visible at the operator-facing surface, not just internal to
   `CrawlerManager`.

4. **Design the Redis-backed media-evidence store** (§5) — separate design
   task, not a mechanical refactor like step 1. Needs an explicit decision
   on:
   - Durability posture (accept Redis-only risk vs. periodic drain to a
     durable store — this audit does not resolve this, it's a product
     tradeoff).
   - Fleet-wide dedup strategy for `media_assets` (a `SADD`-style known-set,
     analogous to the frontier's `urls:known`, is the obvious port but
     needs its own design pass, not an assumption).
   - Whether `sample_jobs`/fingerprinter claiming becomes a Redis work
     queue analogous to the frontier's claim/lease model (a strong
     candidate, given that model already exists and is tested) or something
     simpler.
   - Preserve the `record_media_link`/`record_manifest_variants`/
     `claim_next_sample_job`/`complete_sample_job`/`mark_asset_matched`
     method contract (§5) so `tests/fingerprinter_queue_test.py` and the
     `--claim-sample-job`/`--mark-match` CLI surface keep working against
     the new storage.

5. **Tests to add** (not yet written):
   - Regression tests asserting Redis mode's crawl-time hot path makes zero
     `url_database`/`media_database`-adjacent-`urls`-table calls after
     step 1 (a characterization test locking in the removal, matching this
     codebase's existing pattern of characterization-tests-before-refactor
     from `audit.md` §12).
   - A test confirming the retry-status clobber bug (§1d) is fixed:
     `--unfinished` after a simulated retry-in-backoff correctly reloads
     the URL.
   - A test confirming §1e's failure mode is closed: injecting a SQLite
     fault no longer aborts a page's remaining discovered links (trivially
     true once there's no SQLite call left in that path, but worth a
     regression test given how subtle the original bug was).
   - Once step 4's design exists: the Redis-evidence equivalent of
     `redis_frontier_test.py`'s concurrent-worker/dedup/no-duplicate-claim
     suite, adapted to media assets/sample jobs.

6. **Benchmarks to rerun:** the `sql-persistence-audit.md` §7 per-URL SQL
   statement count, before/after step 1, on a real (not synthetic) crawl —
   should drop from "4-32+ statements/URL" to "0 statements/URL from the
   frontier path, N+1 from media evidence" in Redis mode. The
   `throughput-ceiling-audit.md` frontier-only benchmark doesn't need
   rerunning (it already excluded SQLite); a *new* end-to-end benchmark that
   *does* include real SQLite calls, before/after step 1, would make the
   §8 latency claims concrete against this specific codebase rather than
   the audit's synthetic `sql_bench.py`.

7. **When is SQLite (standalone mode) "complete"?** It already is, per §3 —
   no changes recommended to `--sql` mode itself. "Complete" for the
   purposes of this migration means: step 1 has not touched
   `core/url_frontier.py`/`URLDatabase`'s SQL-mode behavior at all (it
   shouldn't need to), confirmed by the regression tests in step 5 still
   passing unmodified for SQL mode.

8. **When is the crawler "production-Redis-only"?** After step 1 lands
   (mirror removed, correctness bugs closed) and step 4 lands (media
   evidence has a Redis-backed store) — at that point, a Redis-mode crawler
   process legitimately never needs to open a SQLite file except during the
   bounded startup-seeding defer path. Step 1 alone gets the *frontier* to
   this state; full "production-Redis-only" requires step 4 too.

9. **When should the project move to fingerprinting?** Only after step 4,
   not before — building the fingerprinter worker service against
   single-machine local SQLite (`sample_jobs`) now would mean rebuilding
   its data-access layer once the evidence store moves to Redis. The
   existing method-contract preservation goal (§5, step 4) is specifically
   designed to make that transition possible without a second fingerprinter
   rewrite, but only if the fingerprinter isn't built *before* the storage
   underneath it is decided.

---

# FINAL REPORT

```
ORIGINAL INTENT CONFIRMED:
Yes. Redis is, and per every architecture doc in this repo (frontier-adr.md,
REDIS_MULTIWORKER_SUMMARY.md, IMPLEMENTATION_STATUS.md) was always intended
to be, the production distributed frontier. SQLite was never intended as a
permanent Redis-mode dependency. The current code contradicts that intent
in exactly one place (the frontier hot path, §1) and has never contradicted
it in another (media evidence, which was simply never given a Redis-backed
alternative to move to, §5).

REDIS PRODUCTION ARCHITECTURE:
The frontier half is essentially complete: atomic Lua-scripted dedup,
global cross-domain priority, per-domain rate limiting, claim/lease/token
CAS ownership, exponential-backoff retries, and a background reclaim sweep
are all implemented and tested (151+ passing tests across
frontier_redis_failure_semantics_test.py, redis_frontier_test.py,
crawler_manager_seed_failure_semantics_test.py, and the domain_starvation.py
benchmark). Multi-machine sharing already works today by pointing
frontier.redis_host at one central instance -- no code changes needed for
that part of the target architecture. The evidence half (assets,
observations, fingerprint jobs, manifests, results) has NO Redis
implementation at all -- it is the one real gap against the target.

SQLITE ROLE:
Two legitimate roles, going forward: (1) the standalone --sql /
frontier.type=sqlite backend, unchanged, already correct, for
development/testing/offline use; (2) a narrow, bounded, already-built
durable-defer target for startup seeding only (bounded retry + circuit
breaker + defer into the existing --unfinished resume path) when Redis is
unavailable at process start. Everything else SQLite currently does inside
Redis mode's crawl-time hot path is either pure duplication (the two
worker-side status writes per engine) or a temporary stand-in for
not-yet-built Redis-backed evidence storage (MediaEvidenceDatabase) -- not
a third legitimate permanent role.

SQLITE IN REDIS HOT PATH:
Confirmed present and re-verified against current source (not just the
prior audit): CrawlerManager unconditionally constructs URLDatabase,
DomainDatabase, and MediaEvidenceDatabase regardless of frontier.type
(core/crawler_manager.py:69-75); RedisURLFrontier writes to url_database
inline after every add_url/get_next_url/_complete call
(core/redis_frontier.py:457-458,520-521,600-607); every one of the 7
crawler engines independently duplicates a "pending" status write and a
terminal status write against the same url_database, plus writes every
discovered media link to media_database -- all confirmed still live,
line-for-line, in the current tree. Two concrete, currently-live
correctness bugs result: a retry-in-backoff URL gets its correct "queued"
mirror status clobbered back to "failed" by a redundant worker-side write
(core/redis_frontier.py:604 vs. crawler/async_crawler.py:210), and an
uncaught SQLite exception in the unguarded discovered-links loop
(crawler/async_crawler.py:198-199) can silently drop the rest of a page's
links and report an otherwise-successful Redis-side page fetch as failed.

SQLITE MIRROR REQUIRED: NO
(for the frontier's own status/dedup bookkeeping -- Redis is fully
self-sufficient for scheduling, dedup, priority, claims, and retries.
MediaEvidenceDatabase remains required only because no Redis-backed
alternative has been built yet, not because SQLite is architecturally
necessary there.)

REDIS -> SQLITE FALLBACK: NO
(Option B -- auto-switch to SQLite and continue crawling during an outage,
replaying later -- was evaluated and is explicitly not recommended: it
would introduce a second, temporarily-authoritative frontier with its own
dedup/priority/rate-limit/claim semantics per machine, requiring
reconciliation logic that doesn't exist and reintroducing the exact
per-machine-disjoint-state problem the Redis frontier was built to solve.
Option A -- pause new scheduling, preserve in-flight claims via the
existing lease/reclaim mechanism, retry the Redis connection via the
existing poll loop, resume automatically once Redis returns -- is already
fully implemented, tested, and is the correct design.)

SQLITE OUTAGE SPOOL: YES, but narrower than a general-purpose one
(A bounded, one-shot durable-defer mechanism exists today for startup
seeding only -- load_seed_urls/load_unfinished_urls/load_search_query_urls
-- with bounded per-URL retry, a consecutive-failure circuit breaker, and
durable defer into the existing url_database "queued" status, recoverable
via the existing --unfinished resume path with no new replay machinery.
This is NOT extended to newly-discovered URLs during the ongoing crawl
loop, and this audit recommends against building that extension: discovered
links are an unbounded, continuous stream rather than seeding's bounded,
one-shot batch, and the natural self-healing property of web crawling
--reclaimed pages get re-fetched and re-discover their own links-- already
bounds the loss without needing a second, ongoing outbox/reconciliation
system competing for the same SQLite connection this audit recommends
removing from the hot path in the first place.)

MEDIA EVIDENCE STORAGE:
Currently 100% SQLite (storage/media_evidence.db), 100% local-per-machine,
identical in both Redis and SQL crawl modes -- no frontier-type branching
exists anywhere in the media evidence path. Real, authoritative production
output (the crawler's actual anti-piracy findings), not a mirror of
anything else. No aggregation mechanism exists across machines: a
multi-machine fleet today produces N disjoint, un-deduplicated,
un-aggregated media_evidence.db files with no fleet-wide view -- this is
the single most consequential gap against the target production
architecture, and it requires new design work (a Redis-backed work
queue/evidence store), not a mechanical refactor, before it's solved.

FINGERPRINTER DATA FLOW:
Fully defined at the interface level, not yet distributed. The
--claim-sample-job / --mark-match CLI flags in main.py bypass
CrawlerManager entirely and talk straight to MediaEvidenceDatabase --
this is the real, working, already-tested (tests/fingerprinter_queue_test.py)
integration surface a future fingerprinter service would use. Preserve this
method contract (record_media_link, record_manifest_variants,
claim_next_sample_job, complete_sample_job, mark_asset_matched) when
redesigning the storage underneath it for Redis -- the contract is sound,
only its backing store needs to change, and only once a distributed design
exists (do not build the fingerprinter worker against today's
single-machine SQLite sample_jobs table, since that would need a second
rewrite once evidence storage moves to Redis).

REDIS OUTAGE BEHAVIOR:
Already correctly implemented for the ongoing crawl loop: FrontierUnavailable
is raised (never a false empty/done/zero sentinel) from every
RedisURLFrontier method that can fail; schedulers across all 7 crawler
engines catch it, never read it as "idle," and keep polling on the existing
cadence; workers abandon in-flight claims without falsely marking them
complete, relying on lease-expiry reclaim once Redis returns. This is
deliberately unbounded ("polls forever," by explicit design decision already
documented) for the crawl-time layer, and deliberately bounded (retry +
circuit breaker) only for the separate, one-shot startup-seeding layer. No
changes recommended to this behavior.

PERFORMANCE IMPACT OF REMOVING SQLITE FROM REDIS MODE:
Redis's own ceiling (~13.4-13.8K claim+complete ops/sec at 8-16 workers,
measured with SQLite entirely excluded from that benchmark) is already far
beyond what this crawler will ever need once real HTTP fetch/parse work is
in the loop (a mere 10ms of simulated per-URL work drops Redis CPU from
94-96% saturation to 5.9%) -- Redis is not now, and will not become, the
bottleneck. SQLite's current hot-path cost is real and separately measured:
4-32+ synchronous sqlite3 statements per URL, committed individually by
design (not a bug), with a median 8-15us but heavy-tailed-to-162ms commit
latency: a 203x speedup is available simply from batching commits that are
currently unbatched. Removing the two duplicate worker-side writes and the
three RedisURLFrontier-embedded mirror writes eliminates roughly half the
`urls`-table write volume and all of the associated event-loop/thread-pool
occupation from the frontier path; media-evidence writes remain until step 4
of the migration plan gives them a Redis-backed replacement.

MULTI-MACHINE CONCERNS:
Frontier: none found -- the existing Redis keyspace, atomic Lua claim
design, and claim-token CAS model already support N machines x M workers
sharing one Redis instance correctly (confirmed by domain-starvation-audit.md's
1/2/4/8-concurrent-claimer measurements: zero duplicate claims, unchanged
fairness semantics at every concurrency level tested). One operational
caveat, not a bug: domain_scan_limit (K, currently 250) is a fixed bound
that becomes a real visibility-window risk as active-domain count grows
with fleet size -- already measured against this repo's real seed data and
already has operator-facing telemetry logging when exceeded, but no
auto-scaling; a runbook item, not a code defect. Evidence: the real
obstacle -- see MEDIA EVIDENCE STORAGE above.

RECOMMENDED ARCHITECTURE:
Redis = production distributed frontier (already correct) AND, once built,
production distributed media-evidence/work-state store. SQLite = standalone
--sql backend (already correct, unchanged) PLUS the narrow, already-built,
bounded startup-seeding outage-defer mechanism PLUS (temporarily, until the
Redis-backed evidence store exists) the sole storage for media evidence in
both modes. Redis mode's crawl-time hot path should not construct or write
to url_database/domain_database at all once migration step 1 lands.

MIGRATION PHASES:
1. Remove the SQLite mirror from the Redis crawl-time hot path (delete the
   2 duplicate worker-side writes/engine x7 engines, and the 3 mirror writes
   embedded in RedisURLFrontier) -- low risk, mechanical, fixes 2 live
   correctness bugs as a side effect.
2. Keep the startup-seeding durable-defer mechanism exactly as-is.
3. (Optional) formalize --redis/--sql as explicit CLI flags rather than
   config.yaml-only, to make the boundary operator-visible.
4. Design and build a Redis-backed media-evidence/work-state store
   (durability posture, fleet-wide dedup, claim/lease model for sample
   jobs) -- separate design task, not a mechanical refactor; preserve the
   existing MediaEvidenceDatabase method contract.
5. Add regression tests locking in phase 1's removal and the 2 bug fixes;
   add the Redis-evidence equivalent of redis_frontier_test.py's
   concurrent-worker suite once phase 4's design exists.
6. Rerun a real (non-synthetic) end-to-end per-URL SQL-statement-count
   benchmark before/after phase 1 to confirm the measured improvement
   against this specific codebase.
7. --sql/standalone mode is already complete; phase 1 must not change its
   behavior (verified by phase 5's regression tests passing unmodified for
   SQL mode).
8. The crawler is "production-Redis-only" after phase 1 (frontier) AND
   phase 4 (evidence) both land -- phase 1 alone only gets the frontier
   there.
9. Do not begin building the fingerprinter worker service until phase 4's
   evidence-storage design is decided, to avoid a second rewrite once
   evidence storage moves off single-machine SQLite.

CODE CHANGES MADE: NONE
```
