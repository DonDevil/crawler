# SQL / SQLite Persistence Audit (Step 9)

Roadmap position: `Redis outage semantics → failure visibility → domain
starvation → domain scan window → SQLite batching/duplicate-write analysis →
final real-crawler validation → fingerprinter`. This document is the
"SQLite batching / duplicate-write analysis" item flagged as not-yet-started
by [`domain-starvation-audit.md`](domain-starvation-audit.md) §"STOP" and by
[`frontier-redis-failure-semantics.md`](frontier-redis-failure-semantics.md)
§1. It is an **audit only** — evidence gathering and classification, no
code changes. `git status` at the time of writing shows no changes to any
`storage/*.py` or `core/*.py` file from this task.

This document assumes the reader has NOT read prior conversation history and
recovers all needed context from [`audit.md`](audit.md) (original full-repo
map), [`frontier-adr.md`](frontier-adr.md) §10 (SQLite/local-frontier
compatibility contract), and
[`frontier-redis-failure-semantics.md`](frontier-redis-failure-semantics.md)
§4/§11 (the resume-via-`url_database` design intent). Facts already
established there are cited, not re-derived.

---

## 0. Production architecture recap (confirmed, not assumed)

`config.yaml` §`crawler.frontier.type` defaults to `"redis"` (line 15,
`"sqlite"` is present only as a commented-out alternative on line 13).
`core/crawler_manager.py:80-124` constructs `RedisURLFrontier` when
`frontier_type == "redis"`, falling back to the in-memory `URLFrontier`
(logged as `"Using SQLite frontier (single-worker mode)"`) only if the
Redis connection itself fails at construction time. **Redis is the
production frontier**, confirming the brief — this audit does not
second-guess that.

Critically, `CrawlerManager.__init__` (`core/crawler_manager.py:69-75`)
**unconditionally constructs `URLDatabase`, `DomainDatabase`, and
(if `enable_media_evidence`) `MediaEvidenceDatabase` regardless of which
frontier backend is selected**, and always passes `url_database=self.url_database`
into whichever frontier is built (lines 87, 109, 118). So even in full
production Redis mode, every crawler process also opens and writes to local
SQLite files on every run. SQLite is not "used during early development and
now dead" — it is still wired into the hot path today.

---

## 1. SQL architecture — what SQL currently does

Five SQL-bearing modules under `storage/`, all wrapping raw `sqlite3`
(no ORM, no async driver):

| Module | Class | Backing file (config key) | Purpose |
|---|---|---|---|
| `storage/url_database.py` | `URLDatabase` | `storage/crawl_state.db` (`crawler.storage.sqlite_path`) | Per-URL status mirror: dedup/crash-recovery for the frontier |
| `storage/domain_database.py` | `DomainDatabase` | same file as above | Per-domain `score`, written only by the fingerprinter match feedback loop |
| `storage/media_evidence_database.py` | `MediaEvidenceDatabase` | `storage/media_evidence.db` (`crawler.storage.media_sqlite_path`) | **Real production output**: discovered media assets, observation log, sampling job queue, manifest variants |
| `storage/crawl_state_db.py` | `CrawlStateDB` | `storage/crawl_state.db` | Generic key/value `state` table — **dead code** |
| `storage/async_database_writer.py` | `BatchedDatabaseWriter` | n/a (shared helper) | Wraps `execute()`/`commit()` for the three live database classes above |

`storage/result_exporter.py` (`ResultExporter`) does CSV/JSON export, no SQL
at all, and has **zero callers anywhere in the codebase** (verified by
grep) — out of scope for this audit.

`tests/report.py` opens its own **independent** raw `sqlite3.connect("storage/crawl_state.db")`
(line 12, line 111) for read-only reporting queries — a fourth, uncoordinated
access path into the same file, bypassing `URLDatabase` entirely.

`main.py`'s `--claim-sample-job` / `--mark-match` CLI flags talk directly to
`MediaEvidenceDatabase`/`DomainDatabase`, bypassing `CrawlerManager` — this
is the (not-yet-built) fingerprinter service's intended integration point.

**Verified dead code**: `CrawlStateDB` has zero callers anywhere
(`grep -rn "CrawlStateDB" --include=*.py .` outside its own file returns
nothing), and its `state` table does not exist in the live `crawl_state.db`
(confirmed directly: `sqlite_master` lists only `urls` and `domains`).
This matches the same finding already recorded in `audit.md` §2/§3. Not
touched by this audit (no code changes), just re-confirmed current.

---

## 2. Production role of SQL

**Combination, split cleanly by table:**

- `urls` / `domains` (`crawl_state.db`): **local, best-effort, non-authoritative
  mirror**. Written on the same machine that owns the crawler process, never
  shared, never aggregated. Exists purely for (a) startup dedup in the
  single-worker local-frontier fallback and (b) `--unfinished` resume /
  shutdown reporting. Redis is authoritative for live scheduling in
  production; this table is disaster-recovery plumbing, not a queryable
  system of record.
- `media_assets` / `media_observations` / `sample_jobs` / `manifest_variants`
  (`media_evidence.db`): **real production storage**. This is the actual
  anti-piracy output of the crawler — nothing else records discovered
  media. It is local-per-machine today (see §10), which is the most
  consequential production gap this audit found, but it is emphatically
  not disposable/temporary data the way `urls`/`domains` are.
- `state` (`CrawlStateDB`): defined, unused, not wired to anything. Not
  production data at all.

None of the SQL is "development scaffolding now dead" — the brief's
warning not to assume that was correct.

---

## 3. Table inventory

| Table | Purpose | Authoritative? | Written by | Read by | Production-needed? |
|---|---|---|---|---|---|
| `urls` | URL status mirror (`queued→pending→visited\|failed\|skipped`) | No — Redis (or the in-process heap in local mode) is authoritative for scheduling | `core/url_frontier.py:91,141`; `core/redis_frontier.py:458,521,607`; all 6 `crawler/*_crawler.py` engines (`add_url(status="pending")`, `update_status(url,status)`); `core/crawler_manager.py:275` (deferred-seed fallback) | `URLFrontier.add_url`'s `is_visited()` dedup check (`core/url_frontier.py:76`, **local frontier only** — `RedisURLFrontier` never calls `is_visited`); `load_unfinished_urls` (`core/crawler_manager.py:311`); `get_status_counts` (shutdown log, `core/crawler_manager.py:491`); `tests/report.py` | Yes — crash-recovery/resume and reporting, not scheduling |
| `domains` | Per-domain `score` | No | Only `MediaEvidenceDatabase.mark_asset_matched` (`storage/media_evidence_database.py:309-311`), itself only reachable via the `--mark-match` CLI flag | Only `get_score` inside the same method | Only if/when the fingerprinter match-feedback loop is actually exercised — currently **0 rows** in the live db despite 81,873 `urls` rows and 477 media assets (see §12); effectively dormant in practice, not "inert code" the way `audit.md` characterized it (the wiring is real, just unused so far) |
| `media_assets` | One row per discovered media URL, upserted | **Yes** — sole record of discovered media | `record_media_link` (`storage/media_evidence_database.py:98-192`), called from every crawler engine's response/parse path | `list_media_assets`, `claim_next_sample_job`, `complete_sample_job`, fingerprinter CLI side-channel | Yes — this is the product |
| `media_observations` | Append-only log of each time a media URL was (re)discovered | Yes (audit trail) | `record_media_link` (line 160-175), one insert per discovery event | `list_observations` | Yes, for provenance |
| `sample_jobs` | Work queue for the not-yet-built fingerprinter service | Yes | `record_media_link` (upsert), `claim_next_sample_job`, `complete_sample_job`, `update_sample_job_status` | `get_sample_jobs`/`claim_next_sample_job` (CLI side-channel only) | Yes, once the fingerprinter service exists — currently only reachable via manual `--claim-sample-job` CLI runs |
| `manifest_variants` | HLS/DASH manifest variant URLs for a media asset | Yes | `record_manifest_variants` (line 194-217) | `list_manifest_variants` | Yes, niche but real |
| `state` | Generic key/value | N/A | Nobody | Nobody | No — dead |

---

## 4. Per-URL SQL call path

Traced through the actual production configuration: `RedisURLFrontier` +
`AsyncCrawler` (the default `engine: "async"`; all 5 other crawler engines
— `http`, `hybrid`, `playwright`, `selenium`, `tor`, `scrapling` — replicate
the identical `url_database`/`media_database` call sequence at the
equivalent points, confirmed by grep across all 6 files).

```
scheduler task:
  RedisURLFrontier.get_next_url()                         [core/redis_frontier.py:462]
    -> Redis Lua EVALSHA (1 RTT, authoritative claim)
    -> url_database.update_status(url, "pending")          UPDATE + commit   [line 520-521]

worker task:
  url_database.add_url(url, status="pending")              UPSERT + commit  [crawler/async_crawler.py:158-159]
  fetch() -- network I/O, no SQL unless a raw media byte-stream short-circuits:
    media_database.record_media_link(...)                  UPSERT+SELECT+INSERT+UPSERT (3 commits, 1 select) [fetch(), line 97-116]
  parse HTML:
    for each of M media_links:
      media_database.record_media_link(...)                UPSERT+SELECT+INSERT+UPSERT, x M  [worker(), line 181-196]
    for each of N discovered links:
      AsyncFrontier.add_url(link) -> RedisURLFrontier.add_url()
        -> Redis Lua EVALSHA (1 RTT, authoritative dedup+enqueue)
        -> url_database.add_url(cleaned, status="queued")   UPSERT + commit, x N  [core/redis_frontier.py:456-458]
  completion:
    RedisURLFrontier.mark_visited/mark_failed(claim)
      -> Redis Lua EVALSHA (1 RTT, authoritative completion)
      -> url_database.update_status(claim.url, db_status)   UPDATE + commit  [core/redis_frontier.py:600-607]
    url_database.update_status(url, status)                 UPDATE + commit  [crawler/async_crawler.py:209-210] ** DUPLICATE, see §6 **
```

For every SQL operation:

| Op | Type | Rows | Txn boundary | Commit freq | Connection | Thread | Blocks event loop? | Batchable? | Required before URL can continue? |
|---|---|---|---|---|---|---|---|---|---|
| `get_next_url`'s `update_status` | UPDATE | 1 | implicit, per-call | every call (see §5) | shared `URLDatabase._conn` | event loop thread | **yes** | yes | No — advisory mirror only |
| worker's `add_url("pending")` | UPSERT | 1 | implicit, per-call | every call | same | event loop thread | yes | yes | No |
| `record_media_link` (x3 statements) | UPSERT, SELECT, INSERT, UPSERT | 1 each | 3 separate implicit txns per call | 3x per call | shared `MediaEvidenceDatabase._conn` | event loop thread | yes | yes (with a durability tradeoff, see §14) | No, but data-loss-sensitive if skipped (see §14) |
| `add_url("queued")` per discovered link | UPSERT | 1 | implicit, per-call | every call, x N | shared `URLDatabase._conn` | event loop thread | yes | yes | No |
| `_complete`'s `update_status` | UPDATE | 1 | implicit, per-call | every call | shared `URLDatabase._conn` | event loop thread | yes | yes | No |
| worker's terminal `update_status` | UPDATE | 1 | implicit, per-call | every call | shared `URLDatabase._conn` | event loop thread | yes | n/a — should be deleted, not batched (§6) | No |

None of these are `await`ed onto a thread pool anywhere (see §11). All are
plain synchronous `sqlite3` calls executed inline on whatever coroutine
happens to be running.

---

## 5. Operation counts per URL

Using the call path in §4, for `N` = discovered links found on the page and
`M` = media links found on the page, **all figures are against the local
SQLite mirror only — Redis round trips are separate and already counted in
`docs/architecture/throughput-ceiling-audit.md`.**

| URL outcome | SQL UPDATEs | SQL INSERT/UPSERTs (`urls`) | SQL ops (`media_evidence.db`) | Commits (`urls` table) | Total SQL statements |
|---|---|---|---|---|---|
| Successful, 0 links, 0 media | 3 (`pending`x1 + terminal x2, see §6) | 1 (`pending` upsert) | 0 | 4 | 4 |
| Successful, N discovered links, 0 media | 3 | 1 + N | 0 | 4 + N | 4 + N |
| Successful, N links, M media | 3 | 1 + N | 4M (1 select + 3 writes each) | 4 + N | 4 + N + 4M |
| Failed (no retries left) | 3 | 1 | 0 (unless media captured pre-failure) | 4 | 4 |
| Failed (retry scheduled, attempts remain) | 3 | 1 | 0 | 4 | 4 — **but see §6, the status written is wrong** |
| Skipped (blacklisted at claim time) | 1 (`update_status("skipped")`, worker only) | 0 | 0 | 1 | 1 |

A realistic page (config default seed/discovery priorities suggest tens of
links per page is typical for the sites this crawler targets) with, say,
20 discovered links and 2 media links: **4 + 20 + 8 = 32 SQL statements,
32 commits, against local SQLite, for one crawled page.** This is squarely
in the "10+ database operations / URL" regime the brief asked us to check
for, not "1 write / URL."

---

## 6. Duplicate-write findings

### 6.1 TRUE REDUNDANCY — `pending` status written twice per URL

`RedisURLFrontier.get_next_url()` (`core/redis_frontier.py:520-521`) writes
`update_status(url, "pending")` immediately after claiming. `AsyncCrawler.worker()`
(`crawler/async_crawler.py:158-159`) then writes `add_url(url, status="pending")`
before the fetch even starts. Same row, same effective value, back-to-back
within one URL's processing — no state changes between them, no crash
window either write closes that the other doesn't already close. **Pure
waste**: removing either call has zero behavioral effect. (The
`add_url`-vs-`update_status` distinction doesn't matter here either — by
this point in the lifecycle the row already exists from the original
`add_url("queued")`, so the `add_url` call is itself just an upsert to
`"pending"`, functionally identical to `update_status`.)

### 6.2 TRUE REDUNDANCY, WITH A HIDDEN CORRECTNESS BUG — terminal status written twice

`RedisURLFrontier._complete()` (`core/redis_frontier.py:600-607`) computes
`db_status` from the **Redis-side outcome**, which is one of four values:
`visited`, `skipped`, `retry_scheduled → "queued"`, `failed_permanent → "failed"`.
Immediately after, `AsyncCrawler.worker()` (`crawler/async_crawler.py:205-210`)
independently writes its own `status`, which is only ever `"visited"` or
`"failed"` — it has **no concept of `retry_scheduled`**.

For the ordinary visited case these agree and the second write is simple
waste, same as §6.1. But for a failure where Redis still has retries left
(`mark_failed` → Redis returns `"retry_scheduled"`), `_complete` correctly
writes `db_status="queued"` (line 604) — the SQLite mirror correctly shows
this URL as still resumable — and then the worker's very next line
**overwrites it back to `"failed"`** (line 210, since `status` was set to
`"failed"` at line 201 whenever `failure_reason` is truthy, with no
distinction for retry-vs-permanent). The row in `urls` now says `failed`
while Redis is still actively holding it as `retry_scheduled`, due to be
requeued.

**Impact is real but narrow**: it does not affect the currently-running
crawl (Redis, not SQLite, drives the actual retry), and re-crawling a URL
that SQLite thinks already failed is not itself unsafe. It only bites
`load_unfinished_urls()` (`core/crawler_manager.py:311`, which filters
`WHERE status IN ("queued", "pending")`) — on a **fresh process** started
with `--unfinished` while that URL's retry was still in backoff, the URL
would silently not be reloaded (SQLite says `failed`, doesn't match the
filter), even though the original Redis instance genuinely still had it
queued for retry. This is a `retry_scheduled` visibility bug specifically
in the crash-recovery path, not a live-crawl bug. Classify as
**correctness-sensitive**, not simply removable without also fixing the
status the worker writes (or, more simply, deleting the worker's redundant
write entirely and trusting `_complete`'s already-correct mapping).

### 6.3 INTENTIONAL STATE TRANSITION — `queued → pending → visited/failed/skipped`

The three-state SQLite mirror lifecycle itself (as opposed to the duplicate
writes within it) is intentional: `queued` (added to frontier) →
`pending` (claimed by a worker) → terminal. This is not redundant; it is
how `--unfinished` distinguishes "never claimed" from "claimed but the
process died mid-crawl" from "genuinely done." No change warranted.

### 6.4 IDEMPOTENT, NOT REDUNDANT — SQLite mirror duplicating Redis's own dedup

Every `RedisURLFrontier.add_url` call does Redis-side dedup (`SADD urls:known`,
authoritative) **and** a `url_database.add_url` mirror write
(`core/redis_frontier.py:456-458`). This looks like the "duplicate writes
between Redis and SQLite" the brief specifically asks about, and it is one
— but it is not eliminable, because the two copies exist for different
failure domains: Redis dedup is authoritative for the live crawl, and the
SQLite copy is what `load_unfinished_urls()` reads after a **total** Redis
data loss (per the explicit design in
`frontier-redis-failure-semantics.md` §4 point 4 and §11: "`url_database`
(SQLite) already persists per-URL status... A URL the frontier couldn't
accept can be written directly into that same table"). Classify as
**idempotent duplicate, optimizable (batchable/deferrable) but not
removable** — see §14.

### 6.5 SELECT-after-INSERT in `record_media_link` — necessary, not redundant

`storage/media_evidence_database.py:121-158`: an `INSERT ... ON CONFLICT`
upsert into `media_assets`, followed by a `SELECT id FROM media_assets
WHERE url = ?`. This looks like a classic SELECT-before-INSERT anti-pattern
but is actually the reverse and is necessary: `cursor.lastrowid` is only
populated on the fresh-insert branch of an upsert, not the
`ON CONFLICT DO UPDATE` branch, and the caller needs `asset_id` either way
to insert the FK-dependent `media_observations`/`sample_jobs` rows.
Classify as **intentional**, with a low-risk optimization available (SQLite
3.35+'s `RETURNING id` clause would collapse this into the same statement)
— see §14.

---

## 7. Transaction / commit findings

**The central finding of this audit**: `BatchedDatabaseWriter`
(`storage/async_database_writer.py`) is misleadingly named. Its
constructor accepts and stores `batch_size` (default 50), but `execute()`
(lines 39-48) calls `self._flush()` — which commits — **after every single
call**, unconditionally:

```python
def execute(self, sql, params=None):
    with self._lock:
        self._batch.append(WriteOperation(sql=sql, params=params))
        self._flush()          # <-- always, regardless of batch_size
```

`self.batch_size` is read nowhere else in the file. This is not a bug that
was introduced accidentally without comment — the docstring on `execute()`
(lines 40-45) states this is deliberate: *"The crawler needs fresh
visibility for new rows across connections, so each operation is committed
immediately instead of waiting for a large batch to accumulate."* So today
the system does exactly `INSERT / COMMIT` per URL/link/media-record, as the
brief hypothesized, and it does so **by design**, not by oversight — but the
class's name, its `batch_size` parameter, and its docstring's stated
rationale for a `threading.Lock` (implying concurrent batched writers) are
all misleading relative to what the code actually does. This is a genuine
**dead-parameter / misleading-abstraction** finding independent of whether
per-statement commit is the right choice.

**Measured cost** (synthetic benchmark, `/tmp/.../scratchpad/sql_bench.py`,
run against a throwaway WAL-mode db with the exact same PRAGMAs as
production — `synchronous=NORMAL`, `busy_timeout=5000`, `cache_size=10000`,
`temp_store=MEMORY` — never touching `storage/crawl_state.db`):

| Pattern | 2000 ops, total wall time | Throughput | Notes |
|---|---|---|---|
| `execute()` + `commit()` per row (current production behavior) | 653.9 ms | 3,059 ops/s | median 8µs/op, but **max 162.3 ms** on one op — heavy-tailed, WAL-checkpoint/fsync-class stall |
| 1 commit for all 2000 inserts | 3.2 ms | 620,555 ops/s | **203x faster** |
| commit every 50 (i.e. `batch_size` actually honored) | 3.9 ms | 514,551 ops/s | 168x faster |

The per-op median cost (8-15µs) is cheap in isolation, which is why this
hasn't been an obvious problem in local single-worker testing. The real
cost is (a) the aggregate wall-clock time across the 4-32+ SQL statements
per URL counted in §5, all serialized on the event loop (§11), and (b) the
long tail — an 8µs median with a 162ms max means occasional multi-hundred-
millisecond event-loop stalls are already happening today, they're just
invisible unless specifically measured, and they will be materially worse
on networked/cloud-VM disks than the benchmark machine's local disk.

**Is batching safe here?** Only partially, and it depends on which table:

- For `urls` (a resume/dedup mirror, never read synchronously mid-crawl —
  §1): a batch lost on crash just means a few extra URLs get needlessly
  redeferred/re-added on next `--unfinished` run, which Redis will reject
  as duplicates anyway. Low correctness risk.
- For `media_assets`/`media_observations`/`sample_jobs` (real, otherwise-
  unrecoverable output data — §2): a batch lost on crash is a genuine loss
  of a piracy-evidence record with no other copy anywhere. Batching here
  needs an explicit, bounded flush window (time- or size-based), not an
  unbounded accumulate-until-shutdown strategy. Medium correctness risk,
  not "just flip batch_size on."

Full candidate-by-candidate treatment in §14.

---

## 8. Redis/SQL consistency findings

Ordering is uniform and consistent across all three `RedisURLFrontier`
mutating operations — **Redis always executes and succeeds first; SQLite
is written only after Redis confirms**:

```python
# add_url (core/redis_frontier.py:442-460)
result = self._add_url_script(...)      # Redis; raises FrontierUnavailable on failure
if result:
    self.url_database.add_url(...)      # SQLite, only reached if Redis succeeded

# get_next_url (core/redis_frontier.py:485-523)
result = self._claim_next_script(...)   # Redis; raises on failure
...
self.url_database.update_status(url, "pending")   # only reached after a successful claim

# _complete / mark_visited/mark_failed/mark_skipped (core/redis_frontier.py:568-607)
result = self._complete_claim_script(...)   # Redis; raises on failure
...
self.url_database.update_status(claim.url, db_status)   # only reached after Redis completes
```

So **"SQL succeeds → Redis fails"** cannot happen in this code — Redis is
always attempted first and any Redis failure raises `FrontierUnavailable`
before the SQLite line is ever reached. This matches the intended design
recorded in `frontier-redis-failure-semantics.md`.

**"Redis succeeds → SQL fails" is the real, unhandled case, and it is worse
than "the mirror falls slightly behind."** None of the three `url_database.*`
calls above are wrapped in `try/except`. A `sqlite3.OperationalError` (disk
full, `busy_timeout` exceeded, WAL corruption) raised from any of them
propagates as an **uncaught exception out of a frontier method that has
already mutated Redis**. Concretely, in the discovered-links loop
(`crawler/async_crawler.py:198-199`):

```python
for link in links:
    await self.frontier.add_url(link, priority=...)
```

If the SQLite mirror write inside `RedisURLFrontier.add_url` for the 3rd
of 20 discovered links throws, the exception propagates out of this `for`
loop entirely — **links 4-20 are never even offered to Redis**, and they
are lost for good (not retried, not deferred anywhere — this loop has no
per-link error handling). The outer `except Exception` in `worker()`
(`crawler/async_crawler.py:240-249`) then calls `mark_failed` on the
**page's own claim**, marking an otherwise-successful page fetch as failed,
purely because of a local-disk problem on this one machine. This is the
concrete failure scenario the brief asked for: a Redis-side success can be
turned into a reported crawl failure, and a subset of real discovered URLs
silently dropped, by a SQLite-only fault — precisely because the "fallback
persistence" mirror is not actually isolated from the authoritative path
it's supposed to be secondary to. **This is a correctness finding, not a
performance one**, and it is the most significant one in this audit.

Other consequences, all lower severity:
- Lost crawl records: no — `urls` isn't the record of truth, `media_assets`
  is unaffected by this particular failure mode (media writes are already
  individually try/excepted in `worker()`, line 184-196, unlike the
  discovered-links loop).
- Duplicate crawling: no — Redis's own dedup (`urls:known`) is unaffected;
  worst case is a URL gets re-offered and Redis correctly rejects it as
  known.
- Resume problems: yes, as detailed in §6.2 for the `retry_scheduled` case,
  and additionally any URL whose mirror write never landed (this failure
  mode, or ordinary process kill mid-write) simply won't appear in
  `--unfinished` resume — but since Redis is what's actually resumed from
  during a live run (this only matters after **total** Redis loss), this is
  a narrow, already-documented limitation, not new.
- Media records missing from SQL: not from this failure mode specifically
  (see above), but see §14 on batching risk for media writes generally.

---

## 9. Concurrency findings (single machine, multiple workers)

- **One SQLite connection per database class, shared across the whole
  process** — `URLDatabase`, `DomainDatabase`, `MediaEvidenceDatabase` each
  open exactly one `sqlite3.connect(..., check_same_thread=False)` in
  `__init__` and hand it to one `BatchedDatabaseWriter`. `DomainDatabase`
  and `URLDatabase` share the *same underlying file* (`crawl_state.db`) but
  use **two independent connection objects** to it (verified: no
  connection sharing between the two classes) — each with its own WAL/lock
  state, relying on SQLite's own cross-connection file locking rather than
  any application-level coordination.
- **WAL mode, `busy_timeout=5000`, `synchronous=NORMAL`** are set uniformly
  on every connection (`storage/url_database.py:20-25`,
  `storage/domain_database.py:19-22`,
  `storage/media_evidence_database.py:22-27`) — a defensible, deliberate
  configuration for a mostly-single-writer, occasionally-read workload.
  `CrawlStateDB` (dead code, §1) sets WAL but not the other PRAGMAs —
  irrelevant since it's unreferenced.
- **`BatchedDatabaseWriter._lock` is a `threading.Lock`, but nothing in the
  current codebase calls into it from more than one OS thread.** Every
  `url_database`/`media_database` call happens directly on coroutines
  running on the single asyncio event loop thread (confirmed in §11 — no
  `to_thread`/`run_in_executor` wraps any of them). Cooperative scheduling
  means no two coroutines execute SQL "at the same time" regardless of
  `concurrency` (default 25, `config.yaml:3`) — they interleave only at
  `await` points, and no `await` exists inside any `url_database`/
  `media_database` call. **The lock is real but currently uncontended**;
  it is a latent safeguard for a threading model this code does not
  actually use yet, not a bottleneck today.
- **Measured real OS-thread contention** (for context, since the brief asks
  for 1/2/4/8-worker measurements — this simulates what *would* happen if
  SQL calls were ever offloaded to real threads, which they are not today):
  same benchmark harness, N real threads each doing 300 `execute()`+`commit()`
  calls against one shared WAL-mode db:

  | Threads | Total ops | Wall time | Aggregate throughput | Slowest individual worker |
  |---|---|---|---|---|
  | 1 | 300 | 223 ms | 1,343 ops/s | 58 ms |
  | 2 | 600 | 390 ms | 1,537 ops/s | 254 ms |
  | 4 | 1200 | 525 ms | 2,285 ops/s | 394 ms |
  | 8 | 2400 | 570 ms | 4,209 ops/s | 465 ms |

  WAL allows one writer at a time; aggregate throughput scales sub-linearly
  and individual-worker tail latency grows sharply as writers queue behind
  each other. **This is only a risk if/when SQL calls get moved off the
  event loop thread** (§11's own recommendation would create exactly this
  condition) — any such change must serialize writes through a single
  writer (e.g., one dedicated writer thread/queue per database file), not
  naively run `to_thread` per-call from N concurrent workers, or it would
  reproduce this contention for real.

---

## 10. Multi-machine findings

**Multiple crawler machines do NOT share one SQLite file.** Each
`CrawlerManager` resolves `sqlite_path`/`media_sqlite_path` relative to its
own process's base directory (`core/config.py:157-160`, `_resolve_path`) —
there is no configuration path, in `config.yaml` or anywhere in the code,
that points at a network filesystem or a shared host. This is the
*correct* half of the intended design (SQLite as local, not shared,
storage) and does **not** trigger the "STOP, shared SQLite across
machines" red flag the brief warns about — that scenario does not exist in
this codebase.

**However, there is no aggregation mechanism anywhere.** Grep for anything
that reads, ships, syncs, or merges another machine's `crawl_state.db` or
`media_evidence.db` returns nothing — no rsync/S3-upload/replication code,
no cross-machine query layer, nothing in `main.py`, `core/`, or `storage/`.
Per the deployment model in the brief:

```
System A -> SQLite A (crawl_state.db, media_evidence.db)
System B -> SQLite B
System C -> SQLite C
```

For `urls`/`domains`, this is low-stakes — each machine's mirror only needs
to see its own history for `--unfinished` resume, and Redis is the
cross-machine coordination layer that actually matters for scheduling.

**For `media_assets`/`media_observations`/`sample_jobs`/`manifest_variants`,
this is the single most consequential production gap this audit found.**
This is real, otherwise-unrecoverable output data (§2, §3), and under the
intended multi-machine production architecture, a fleet of N crawler
machines produces N disjoint, un-aggregated SQLite files of piracy
evidence, with no fleet-wide view, no dedup across machines (a URL
discovered independently by two machines becomes two separate
`media_assets` rows in two separate files), and no path for the (not yet
built) fingerprinter service to see the whole fleet's backlog — the
`--claim-sample-job` CLI flag only ever sees the local machine's
`sample_jobs` table. This is an architecture gap to flag for a design
decision, not something to fix as part of this audit (per the brief:
"do not invent an aggregation mechanism if none exists").

---

## 11. Async/event-loop findings

**Every SQL call in the crawl hot path is a synchronous `sqlite3` call
executed directly on the asyncio event loop thread. None are offloaded.**

Verified by grep across all 6 crawler engines
(`crawler/{async,http,hybrid,playwright,selenium,scrapling,tor}_crawler.py`)
for `to_thread`/`run_in_executor` co-occurring with `url_database`/
`media_database`: the only `to_thread` calls in these files wrap
**fetch-side** work — `selenium_crawler.py:130` (`self._fetch_sync`),
`scrapling_crawler.py:114` (`self._fetch_sync`), `hybrid_crawler.py:150-151`
(Selenium driver lifecycle). Every `url_database.add_url`/`update_status`
and `media_database.record_media_link`/`record_manifest_variants` call in
every engine is a bare synchronous call, not `await`ed, not wrapped.

This matters concretely because of §7's measured tail latency: a median
8-15µs SQLite commit is invisible, but the same benchmark showed a 162ms
outlier on an otherwise-identical operation. At `concurrency=25` (default),
one such stall blocks all 25 workers' fetch/parse/Redis-claim progress for
its duration, since nothing else on that thread can run until the
synchronous call returns. `AsyncFrontier` (`core/frontier_executor.py`)
already solved exactly this problem for the Redis calls — it offloads any
non-`URLFrontier` backend to `asyncio.to_thread` specifically because
"`RedisURLFrontier` ... performs blocking network I/O through a synchronous
`redis-py` client and must never run directly on the event loop thread"
(`core/frontier_executor.py:9-11`). The same reasoning applies to the
synchronous `sqlite3` calls in `url_database`/`media_database`, and no
equivalent adapter exists for them today — an inconsistency between how
the two blocking I/O dependencies in this codebase are treated. See §14
for whether offloading is actually the right fix (it introduces the
concurrency risk quantified in §9, not a free win).

---

## 12. Query/index findings

Schema as-is (no indexes beyond `PRIMARY KEY`/`UNIQUE` constraints anywhere
in any `CREATE TABLE`):

| Table | PK / UNIQUE | Hot-path queries that filter/sort on it | Indexed? | Verdict |
|---|---|---|---|---|
| `urls` | PK `url` | `is_visited(url)` (PK lookup, fast); `update_status`/`add_url` (PK lookup, fast) | Yes, via PK | Fine — every per-URL hot-path query hits the PK |
| `urls` | — | `get_status_counts()` (`GROUP BY status`); `get_urls_by_status`/`get_urls_and_statuses` (`WHERE status IN (...) ORDER BY last_seen`) | No index on `status` or `last_seen` | Full scan, but **not hot-path** — only called at startup (`--unfinished`) and shutdown (one log line). At 81,873 measured rows (§13) this is cheap. LOW priority. |
| `domains` | PK `domain` | `get_score(domain)` | Yes | Fine, and table is empty in practice (§3) |
| `media_assets` | PK `id`, UNIQUE `url` | `record_media_link`'s upsert + id lookup (both hit `url` UNIQUE) | Yes | Fine |
| `media_assets` | — | `list_media_assets()` (`ORDER BY last_seen`, full scan) | No | Not hot-path (CLI/reporting only) |
| `sample_jobs` | UNIQUE `asset_id` | `claim_next_sample_job` → `get_sample_jobs(statuses=["pending"])` → `WHERE status IN (...) ORDER BY priority, updated_at`, **entire result materialized into a Python list, then `[0]` is taken** instead of `LIMIT 1` in SQL | No index on `status` | Not hot-path today (CLI-only, one job at a time), but the `LIMIT 1`-in-Python-not-SQL pattern is a real inefficiency that will degrade linearly as the pending backlog grows — worth fixing whenever the fingerprinter service starts calling this in a loop, not urgent now (currently 422 pending rows, §13). |
| `media_observations` | — | `list_observations(asset_id)` (no index on `asset_id` FK) | No | Full scan; 650 rows today, growth-dependent (§13), CLI-only |
| `manifest_variants` | UNIQUE `(asset_id, variant_url)` | `list_manifest_variants(asset_id)` | Yes (composite UNIQUE covers the lookup) | Fine |

No `EXPLAIN QUERY PLAN` surprises beyond the absence of secondary indexes
already listed above — none of the missing indexes sit on an actual
per-URL hot-path query, so this audit does not classify any of them as
urgent. All are "fix if/when this query starts running in a loop at scale,"
matching the brief's instruction not to index columns "because they sound
useful."

---

## 13. Database growth

Measured directly against the live, non-synthetic databases in this repo
(read-only `SELECT COUNT(*)` / file size, no writes):

| File | Size | Rows | Breakdown |
|---|---|---|---|
| `storage/crawl_state.db` | 23,343,104 bytes (22.3 MB) | `urls`: 81,873; `domains`: 0 | status: `pending`=65,688, `visited`=11,192, `failed`=2,352, `skipped`=2,009, `queued`=632 |
| `storage/media_evidence.db` | 5,660,672 bytes (5.4 MB) | `media_assets`=477, `media_observations`=650, `sample_jobs`=477, `manifest_variants`=0 | asset status: `queued_for_sampling`=457, `uncertain_manual_review`=13, `no_match_pending_review`=7 |

No `.db-wal`/`.db-shm` files present (WAL fully checkpointed at rest) — file
sizes above are the genuine steady-state size, not inflated by
uncheckpointed WAL.

**≈285 bytes/row for `urls`** (22.3MB / 81,873), extrapolating linearly (no
index beyond PK, so growth is close to linear in row count):
~2.8MB per 10K URLs, ~28MB per 100K, ~280MB per 1M. Not alarming in
isolation for a single machine, but see the hygiene problem below, which
compounds it.

**A concrete, already-manifested growth/hygiene risk**: 65,688 of 81,873
`urls` rows (80%) are stuck at `pending` — vastly more than the 11,192
genuinely `visited`. `pending` is written when a worker claims a URL
(§4/§6.1) and is only ever resolved to a terminal status by the same
worker completing normally; there is no separate cleanup/expiry for a
`pending` row left behind by a killed/crashed process (SIGKILL, OOM,
`docker stop` without graceful shutdown, etc. — anything that skips the
`finally` block in `CrawlerManager.run()`, `core/crawler_manager.py:485-502`).
This 80%-`pending` figure in the live repo is direct evidence this already
happens in practice, not a hypothetical. These rows are not actively
harmful today (no index scan reads them on the hot path, per §12), but they
accumulate indefinitely across every run that doesn't shut down cleanly,
and `load_unfinished_urls()` reloads all `queued`+`pending` rows verbatim
on the next `--unfinished` run (`core/crawler_manager.py:311`) — so this
backlog only grows, never self-heals.

**No retention/cleanup/VACUUM anywhere.** No `PRAGMA auto_vacuum`, no
`DELETE ... WHERE status IN (...) AND last_seen < ...`, no scheduled
maintenance. SQLite's default `auto_vacuum=none` means deleted rows (there
are essentially none being deleted today — `clear()` is a full wipe, only
called via `--clear-db`) leave free pages the file reuses but never
shrinks from. For a long-running, multi-day, multi-run fleet deployment,
extrapolating the observed 80%-stuck-`pending` pattern: **`urls` growth is
effectively unbounded and increasingly dominated by garbage from
incompletely-shut-down runs**, not by genuine crawl progress. This is a
real risk for long-running deployments, not a hypothetical one — flagged
per the brief's "identify the risks, don't redesign retention yet."

`media_evidence.db` shows no comparable pathology (`sample_jobs` status
breakdown is coherent: 422 genuinely `pending`, the rest in terminal
review/reject states) — the growth risk is specific to `urls`, not media
evidence.

---

## 14. Optimization candidates

| Candidate | Current frequency | Rank | Why |
|---|---|---|---|
| Remove worker's redundant `add_url("pending")` (§6.1) and terminal `update_status` (§6.2) | 2 extra commits / URL, always | **HIGH IMPACT** | Cuts baseline `urls`-table writes from 4 to 2 per URL (50%) with zero behavior change for the common case, *and* fixes the `retry_scheduled` status-clobbering correctness bug in §6.2 as a side effect. Low complexity (delete 2 call sites; the failed-with-retry case additionally needs the worker to stop writing its own `status` and trust `_complete`'s return value, or stop writing entirely). |
| Fix `BatchedDatabaseWriter` to honor `batch_size` (or otherwise batch discovered-link/media writes per page instead of per statement) | 1 commit / statement today, could be 1 commit / page | **HIGH IMPACT** | 203x measured speedup for the `urls` table specifically (§7); directly addresses the brief's central question. Medium complexity: needs an explicit flush policy (size- and/or time-bounded, not "accumulate forever") and an explicit decision on acceptable data loss on crash — different for `urls` (low risk, resume-only) vs. `media_*` (real output data, needs a tighter flush window). This is a durability-semantics decision, not just a perf knob — do not treat it as a drop-in default-on change. |
| Batch discovered-URL mirror writes (`RedisURLFrontier.add_url`'s SQLite half) per page instead of per link | N commits / page today | **HIGH IMPACT** | Same underlying fix as above, isolated to the highest-volume call site (N discovered links per page, §5). Low correctness risk — this table is advisory-only (§7). |
| Batch `media_database.record_media_link` writes per page | 3 commits + 1 select / media link today | **MEDIUM IMPACT** | Real throughput win, but this is production output data, not a mirror (§2) — batching trades some crash-durability for throughput on data with no other copy. Needs a bounded flush window, and probably shouldn't defer past the page boundary it's already scoped to. Medium complexity, medium correctness risk. |
| Wrap the discovered-links loop's SQLite mirror write in its own `try/except` so one bad link doesn't drop the rest of the page (§8) | N/A — one-time code change | **HIGH IMPACT (correctness, not perf)** | Directly closes the concrete failure scenario in §8: a local-disk SQLite fault currently can turn a successful page fetch into a reported failure and silently drop not-yet-added discovered links. Low complexity, low risk — this narrows blast radius, it doesn't change any happy-path behavior. |
| Fix `sample_jobs`'s `claim_next_sample_job` to use `LIMIT 1` in SQL instead of materializing the full pending set in Python (§12) | 1 full-table fetch / claim call | **LOW IMPACT today, grows over time** | CLI-only, 422 rows today. Fix whenever the fingerprinter service starts calling this in a real loop; not urgent. |
| Offload `url_database`/`media_database` calls to `asyncio.to_thread`, matching how `RedisURLFrontier` is already offloaded (§11) | Every call, today on the event loop thread | **MEDIUM IMPACT, CONDITIONAL** | Removes the event-loop-blocking risk quantified in §7/§11, but only *after* the write-volume-per-URL is fixed first (items above) — offloading 32 blocking calls/page to a thread pool without fixing the commit-per-statement problem just moves the same stall count off the event loop without reducing it, and (per §9) naive per-call `to_thread` from many workers would introduce real multi-writer contention that doesn't exist today. Do this only after batching, and route through a single writer, not N-way `to_thread`. |
| Add `status`/`last_seen` indexes on `urls`, `status` on `sample_jobs`, `asset_id` on `media_observations` (§12) | N/A | **LOW IMPACT / NOT WORTH IT YET** | None of these sit on a hot-path query today; all affected call sites are startup/shutdown/CLI-only at current row counts. Revisit only if row counts grow by 10-100x or these queries start running per-URL. |
| Address `urls`-table `pending` accumulation from ungraceful shutdowns (§13) | Ongoing, unbounded | **MEDIUM IMPACT, LONGER-TERM** | Real, already-manifested (80% of the live table). Not a batching question — needs either a TTL/cleanup pass or a way to distinguish "abandoned pending" from "genuinely in-flight" (e.g. a claimed-at timestamp + staleness threshold, mirroring Redis's own lease-expiry concept). Out of scope to design here per the brief; flagging for a future retention decision. |
| Collapse `record_media_link`'s INSERT+SELECT into one round trip via `RETURNING id` (§6.5) | 1 extra SELECT / media link | **LOW IMPACT** | Real but small (1 read query saved per media link); SQLite version dependency should be checked before relying on `RETURNING` (3.35+, released 2021). |
| Delete dead `CrawlStateDB`/`storage/crawl_state_db.py` and unused `storage/result_exporter.py` | N/A | **NOT WORTH OPTIMIZING (just delete)** | Zero callers, zero runtime cost today, but pure maintenance debt — out of scope for this audit's "no code changes" mandate, noted for a future cleanup pass. |
| Wire `DomainDatabase.score` into actual crawl-time prioritization/rate-limiting, or formally retire it | 0 writes in practice today (§3) | **NOT WORTH OPTIMIZING NOW** | Not a performance question — it's a product decision about whether the fingerprinter match-feedback loop is meant to influence live crawling. Flagging per `audit.md`'s existing open question, not re-deciding it here. |

---

## 15. Recommended changes (NOT implemented — audit only)

In priority order, each with benefit / correctness risk / complexity /
production-scalability impact:

1. **Remove the two redundant `urls`-table writes in `crawler/async_crawler.py`
   (and the equivalent lines in the other 5 engines) — §6.1/§6.2.**
   - Benefit: ~50% fewer `urls` writes per URL in the baseline case; fixes
     the `retry_scheduled` status bug as a side effect.
   - Correctness risk: low, but requires care — the fix must decide what
     the worker-side write should become for the retry-scheduled-failure
     case (delete it entirely and trust `_complete`'s mapping, most likely),
     not just delete both lines blindly.
   - Complexity: low — same pattern repeated across 6 files.
   - Production scalability: helps every machine in the fleet equally,
     independent of worker count.

2. **Isolate the discovered-links loop's per-link SQLite mirror write with
   its own try/except — §8.** Closes the concrete "one bad disk write
   drops the rest of the page's links and reports a false failure" scenario.
   - Benefit: correctness, not throughput — closes a real failure mode.
   - Risk: none — this narrows blast radius without changing happy-path
     behavior.
   - Complexity: low.
   - Scalability: matters more, not less, at fleet scale — more machines
     means more chances to hit a local-disk hiccup on any given machine.

3. **Give `BatchedDatabaseWriter` a real, bounded batching policy** —
   separately tuned for `urls` (low durability requirement, batch freely,
   e.g. per-page or every N ms) vs. `media_*` (real output data, tighter
   bound, e.g. flush every page or every few records, not "accumulate
   until shutdown").
   - Benefit: the 203x measured speedup (§7), applied where it's safe.
   - Risk: medium — this is a durability-semantics change, needs explicit
     sign-off on what's acceptable to lose on an ungraceful crash for each
     table.
   - Complexity: medium — needs a flush trigger (size/time), not just
     "raise batch_size."
   - Scalability: reduces the SQL-induced portion of per-URL latency
     across the whole fleet; does not touch Redis's already-separate
     ceiling (`throughput-ceiling-audit.md`).

4. **Only after (3): consider offloading `url_database`/`media_database`
   calls off the event loop, through a single serialized writer (not naive
   per-call `to_thread`) — §11.**
   - Benefit: removes the event-loop-stall risk quantified in §7/§11.
   - Risk: medium — must avoid introducing the multi-writer contention
     shown in §9; a single dedicated writer thread/queue per db file, not
     N-way concurrent `to_thread`, is the safer shape.
   - Complexity: medium-high — new coordination primitive, more surface
     area than (1)-(3).
   - Scalability: matters most at high `concurrency` settings; low payoff
     if attempted before (3) fixes the write volume itself.

5. **Longer-term, separate decision: address the `urls`-table `pending`
   accumulation from ungraceful shutdowns (§13), and decide on a
   media-evidence aggregation strategy across fleet machines (§10).**
   Both are real, both are out of scope for a batching/duplicate-write
   fix, both need their own design pass before implementation.

Everything else in §14 ranked MEDIUM/LOW/NOT-WORTH-IT is optional
follow-up, not blocking.

---

# FINAL REPORT

```
SQL PRODUCTION ROLE: Combination — urls/domains (crawl_state.db) are a local, non-authoritative, best-effort crash-recovery/resume mirror of the Redis-authoritative frontier; media_assets/media_observations/sample_jobs/manifest_variants (media_evidence.db) are real, authoritative production output data (the crawler's actual anti-piracy findings) with no other copy anywhere. Redis is confirmed as the production frontier; SQLite is not, and is not obsolete.

DATABASE TOPOLOGY: Local per-machine, not shared. Each crawler process resolves its own sqlite_path/media_sqlite_path from config; no code path syncs, ships, or aggregates SQLite files across machines. No shared-SQLite-across-machines antipattern exists (the correct half of the design), but no fleet-wide aggregation of media evidence exists either (the consequential gap).

SQL OPERATIONS PER URL: ~4 statements minimum (urls table only, 0 discovered links, 0 media) up to 4 + N + 4M for N discovered links and M media links -- e.g. ~32 statements for a page with 20 links and 2 media items. Not "1 write / URL" -- squarely in the 10+ operations/URL regime.

COMMITS PER URL: Same count as statements above -- BatchedDatabaseWriter commits after every single execute() call, unconditionally, by design (its docstring states this explicitly), despite being named/parameterized as if it batches. batch_size is stored but never read.

MAIN BOTTLENECK: Per-statement commit overhead on the urls-table mirror and the media_evidence writes -- measured 203x speedup (654ms -> 3.2ms for 2000 ops) switching from per-row commit to a single batched commit, plus a heavy-tailed commit latency (median 8-15us, observed max 162ms) that fully blocks the asyncio event loop since no SQL call is offloaded to a thread.

DUPLICATE WRITE PROBLEMS: Two true redundancies per URL in crawler/async_crawler.py (and the same pattern in all 5 other crawler engines): (1) "pending" status written twice back-to-back (frontier's get_next_url, then the worker's add_url) with no state change between them; (2) terminal status written twice (frontier's _complete, then the worker), where the second write also overwrites a correct "queued" (retry-scheduled) status with an incorrect "failed" status -- a real, if narrow, resume-visibility correctness bug, not just waste.

BATCHING OPPORTUNITIES: HIGH-value and low-risk for the urls-table mirror (advisory-only, never read mid-crawl) -- discovered-link writes and pending/terminal status writes can safely batch per page. MEDIUM-value, higher-risk for media_evidence writes, since that data has no other copy and a lost batch on crash is a genuine, unrecoverable loss of evidence -- needs a bounded flush window, not unbounded accumulation.

SQLITE CONCURRENCY RISK: Low today -- BatchedDatabaseWriter's threading.Lock is real but uncontended, since every SQL call runs on the single asyncio event loop thread, never on a real OS thread. Becomes a real risk (measured: aggregate throughput plateaus at 1,343-4,209 ops/s across 1-8 real concurrent writer threads, with individual worker latency growing to 250-465ms under contention) only if SQL calls are ever offloaded to a thread pool without first serializing them through a single writer.

MULTI-MACHINE RISK: No shared-SQLite-file risk (each machine has its own local files, as intended). The real risk is the opposite: no aggregation mechanism exists for media_evidence.db across machines, meaning a multi-machine fleet produces N disjoint, un-deduplicated, un-aggregated copies of the crawler's actual output data, with no fleet-wide view for the (not yet built) fingerprinter service.

EVENT-LOOP BLOCKING: Confirmed, universal. No url_database/media_database call in any of the 6 crawler engines is wrapped in asyncio.to_thread or run_in_executor -- every one is a synchronous sqlite3 call executed directly on the event loop thread, unlike RedisURLFrontier's calls, which already are offloaded via AsyncFrontier for exactly this reason.

INDEX PROBLEMS: None on the actual per-URL hot path (all hot-path lookups hit a PRIMARY KEY or UNIQUE constraint). Missing status/last_seen/asset_id indexes exist only on startup/shutdown/CLI-only queries at current row counts (81,873 / 477 / 650) -- not urgent, and claim_next_sample_job's Python-side "take [0] of a full result set" instead of SQL LIMIT 1 is a real but currently-cheap inefficiency to fix once the fingerprinter service is built.

DATABASE GROWTH RISK: Real and already manifesting, not hypothetical -- the live crawl_state.db (81,873 urls rows, 22.3MB) shows 80% of rows stuck at "pending" (65,688 of 81,873, vs. only 11,192 "visited"), consistent with accumulation from ungracefully-terminated runs that never resolve their in-flight claims to a terminal status. No cleanup/TTL/retention mechanism exists for this. media_evidence.db (5.4MB, 477 assets) shows no equivalent pathology.

HIGH-IMPACT OPTIMIZATIONS: (1) delete the two redundant urls-table writes per URL, which also fixes the retry-status correctness bug; (2) isolate the discovered-links loop's SQLite mirror write in its own try/except so a local-disk fault can't silently drop the rest of a page's links and report a false failure; (3) give BatchedDatabaseWriter a real bounded batching policy (differentiated by table's durability needs), which measured at 203x for the urls table specifically.

RECOMMENDED NEXT CHANGE: Fix the two redundant urls-table writes (§6.1/§6.2) and the discovered-links try/except isolation (§8) first -- both are low-complexity, low-risk, and one is a genuine correctness bug, not just an optimization. Defer the BatchedDatabaseWriter batching-policy change and any event-loop-offload change to a follow-up task with explicit sign-off on the durability tradeoff for media_evidence.db, per this audit's "measure before optimizing" mandate.

CHANGES IMPLEMENTED: NONE
```
