# `--clear-db` Backend-Aware Semantics — Implementation Record

Status: **implemented and verified**. Follow-through on
[`clear-db-redis-gap-audit.md`](clear-db-redis-gap-audit.md) (§7, "Required
fix"). Scope, restated from the brief that drove this work: make
`--clear-db` clear whichever frontier/persistence backend is actually
active, using the existing `self.frontier.clear()` API — no new CLI flag,
no Redis+SQLite dual persistence, no Lua/claim/retry/scheduler changes, no
automatic Redis crash recovery.

## 1. Why the old behavior was incorrect

`CrawlerManager.clear_storage()` (`core/crawler_manager.py`) called
`self.url_database.clear()` and `self.domain_database.clear()`
unconditionally, regardless of which frontier backend
`config.crawler.frontier.type` selected. For a Redis-backed run this did
nothing to Redis: `self.frontier.clear()` (`RedisURLFrontier.clear()`,
`core/redis_frontier.py:768`, already implemented correctly) was never
called. A run started with `--clear-db` on the Redis backend therefore
inherited every key ever written to that Redis db/namespace across the
frontier's entire history, which is exactly what the linked audit traced as
the root cause of an invariant violation (`visited ∩ failed_permanent ≠ ∅`)
in an overnight run's report.

## 2. How backend selection actually works

There is no `--frontier`/`--redis`/`--sql` CLI flag. The active backend is
selected once, at `CrawlerManager.__init__` time, from
`config.crawler.frontier.type` (`"sqlite"` or `"redis"`,
`core/config.py::FrontierConfig`). SQLite and Redis are alternative
persistence modes for the same abstraction, not a local mirror of a
distributed store — Redis mode's per-URL SQLite mirroring was already
removed in [`phase1-redis-sqlite-mirror-removal.md`](phase1-redis-sqlite-mirror-removal.md).
This task does not reopen that decision.

One wrinkle: if `config.crawler.frontier.type == "redis"` but Redis is
unreachable at construction time, `__init__` catches the exception and
falls back to the local `URLFrontier` (SQLite-backed, single-worker mode)
— see `core/crawler_manager.py:150-164`. So `self.frontier`'s *actual
runtime type* is the only reliable signal of which backend is active;
re-reading `config.crawler.frontier.type` in `clear_storage()` would get
this fallback case wrong. `clear_storage()` therefore branches on
`isinstance(self.frontier, RedisURLFrontier)` rather than the config
string — the same "trust the instantiated object" principle the existing
`hasattr(self.frontier, "reclaim_and_promote")` gate uses elsewhere in this
file for the recovery task. A plain `hasattr(self.frontier, "clear")` gate
would *not* work here: both `URLFrontier` and `RedisURLFrontier` define
`clear()`, but `URLFrontier.clear()` only resets its own in-memory queues
("`url_database` is owned by the caller" — `core/url_frontier.py:293`) and
never touches the persisted SQLite tables, so `hasattr` can't distinguish
the two.

## 3. Exact `--clear-db` semantics

**SQLite mode** (`self.frontier` is `URLFrontier`, whether by config or by
Redis-unavailable fallback): unchanged from before this task —
`self.url_database.clear()` + `self.domain_database.clear()`, i.e. the
persisted SQLite tables backing the local frontier and per-domain scoring.

**Redis mode** (`self.frontier` is `RedisURLFrontier`): `self.frontier.clear()`
is called instead, which `SCAN`s and deletes every `{namespace}:*` key in
the configured Redis db (`core/redis_frontier.py:768-782`, unchanged by
this task — the audit found this method already correct, it just was never
reached). `url_database`/`domain_database` are **not** cleared in this
branch (see §4).

**Both modes**: `self.media_database.clear()` (SQLite or Redis, per
`config.crawler.media_evidence.type`) still runs unconditionally, as
before — see §4.

## 4. Auxiliary-storage distinction

`clear_storage()` now distinguishes two categories of storage:

- **Active frontier persistence** — `url_database` + `domain_database` in
  SQLite mode, or the Redis frontier's own keys in Redis mode. These two
  SQLite tables share one file (`config.crawler.storage.sqlite_path`) with
  no independent backend selection of their own; they *are* the SQLite
  backend, not a separate concern from it. Only one side is cleared,
  matching whichever backend is actually running.

- **Independent auxiliary storage** — `media_database`
  (`build_media_evidence_store`, `core/crawler_manager.py:53`), which has
  its *own* backend selection (`config.crawler.media_evidence.type`,
  independent of `config.crawler.frontier.type`) and is deliberately
  cleared regardless of which frontier backend is active, exactly as
  before this task. This task does not touch `media_database` handling.

One deliberate non-obvious case: in Redis mode, `url_database` is still
constructed and still receives writes — but only as the documented
emergency fallback for URLs that couldn't be enqueued during a transient
Redis outage (`CrawlerManager._make_seed_url_adder`, retried and then
persisted to `url_database` with status `"queued"` for a later
`--unfinished` run; see `docs/architecture/frontier-redis-failure-semantics.md`).
That fallback data is intentionally *not* cleared by a Redis-mode
`--clear-db` — it isn't the active frontier's own persistence, it's
recovery bookkeeping for a different failure mode, and wiping it on every
`--clear-db` would defeat its purpose. Regression test
`test_clear_db_does_not_unnecessarily_clear_sqlite` (below) covers exactly
this.

## 5. Why Redis+SQLite mirroring is not the intended fix

The obvious-looking alternative — "just clear both, to be safe" — was
rejected. SQLite and Redis are alternative backends, never used
simultaneously as primary/mirror per
[`redis-sqlite-boundary-decision.md`](redis-sqlite-boundary-decision.md) and
its Phase 1 follow-through. Unconditionally clearing SQLite in Redis mode
would silently destroy the `_make_seed_url_adder` fallback data described
above (and any independent SQLite-mode data a user might have from a prior
config), for no benefit — Redis mode never reads `url_database` as its
frontier. Unconditionally clearing Redis in SQLite mode would require a
Redis connection to exist even when the crawler is configured not to use
Redis at all.

## 6. Tests performed

New file `tests/clear_db_backend_semantics_test.py` (6 tests, all passing).
Redis-backed tests use a live Redis on `localhost:6379`, `db=1` (never the
production `db=0`), with a fresh UUID-suffixed namespace per test, and skip
(rather than fail) if Redis is unreachable — same pattern as
`tests/crawler_manager_recovery_test.py`. No production Redis namespace or
key was read or modified.

| Test | Verifies |
|---|---|
| `test_clear_db_clears_sqlite_crawler_state` | SQLite config + `--clear-db` → `url_database`/`domain_database` cleared |
| `test_clear_db_does_not_touch_redis` | SQLite config + `--clear-db` → an unrelated Redis key is left untouched |
| `test_without_clear_db_sqlite_state_is_untouched` | SQLite config, no `--clear-db` → SQLite state persists |
| `test_clear_db_clears_redis_namespace` | Redis config + `--clear-db` → `frontier.get_status_counts()` all zero |
| `test_clear_db_does_not_unnecessarily_clear_sqlite` | Redis config + `--clear-db` → a pre-existing `url_database` fallback entry survives |
| `test_without_clear_db_redis_state_is_untouched` | Redis config, no `--clear-db` → Redis state persists |

Also re-ran, unmodified, all passing:

```
tests/manager_test.py
tests/crawler_manager_recovery_test.py
tests/crawler_manager_seed_failure_semantics_test.py
tests/domain_scan_limit_config_test.py
```

(28 passed — confirms normal seeding/resume/recovery-task behavior is
unaffected by the `clear_storage()` change.)

## 7. Follow-up (explicitly not done here)

The originating audit (§5 of `clear-db-redis-gap-audit.md`) separately
identified that `RedisURLFrontier.reclaim_and_promote()` — which sweeps
abandoned inflight leases and promotes due retries after a crash — is
implemented but not wired to run automatically anywhere outside
`CrawlerManager._recovery_loop()` (which only runs while `run()` is active,
i.e. not as a startup recovery step before a fresh run begins). Wiring
automatic Redis crash recovery is out of scope for this task and is left as
a separate follow-up.
