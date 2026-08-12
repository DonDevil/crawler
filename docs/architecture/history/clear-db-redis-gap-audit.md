# `--clear-db` Does Not Clear Redis — Invariant Violation Audit

Status: **audit only — no production code changed, no Redis state modified.**
This document records the investigation into an overnight Redis crawl run
(started via `python main.py ... --clear-db`, backend Redis, namespace
`crawler`, db 0) whose `python tests/report.py --redis` output tripped the
`visited + failed_permanent + skipped > discovered_total` invariant check in
`tests/report_lib.py::terminal_states_are_consistent`, forcing percentages to
report `N/A`. This is the same symptom already logged, but not root-caused,
in the "Redis Overnight Run — 2026-08-10/11" entry of
`docs/architecture/history/test-analysis.md`.

Report at time of investigation:

```
discovered:          39761
visited:              14890
failed_permanent:     38628
queued:                   0
inflight:               228
retry_scheduled:         73
```

## 1. Root cause

**`CrawlerManager.clear_storage()` (`core/crawler_manager.py:248-256`) never
clears Redis.** It only calls `self.url_database.clear()`,
`self.domain_database.clear()`, and (if configured)
`self.media_database.clear()` — all SQLite-backed. It never calls
`self.frontier.clear()`. `RedisURLFrontier.clear()`
(`core/redis_frontier.py:768`) is implemented correctly (SCANs and deletes
every `{namespace}:*` key), but for a Redis-backed run `--clear-db` never
reaches it. So a run "started after `--clear-db`" inherits **every** key ever
written to that Redis db/namespace, across the frontier's entire schema
history — not just the current run's data.

This matters because the frontier's *very first* Redis schema (commit
`97c64b2`, "implemented redis, distributed crawling support") had no
`urls:known` concept at all. Dedup was tracked via a `urls:queued` SET, and
completions wrote **directly** into `{ns}:urls:visited` via `SADD`
(`core/redis_frontier.py` history, lines ~157/310/331 in that commit),
bypassing anything resembling a `known` gate. `{ns}:urls:visited` is the
exact same key name the current schema still uses. Since it was never
cleared, legacy-schema visited entries and current-schema visited entries
coexist in one set.

Because `add_url()`'s dedup check (`_add_url_script`) is only against
`known`, never against `visited`, a URL that's in legacy `visited` but *not*
in `known` is freely re-discovered and re-crawled by current-schema code —
and can now land in `failed_permanent` too, producing an overlap that is
structurally impossible for single-generation, current-schema-only data.

## 2. Redis evidence (read-only inspection, db 0, namespace `crawler`)

```
SCARD known             39761
SCARD visited           14890
SCARD failed_permanent  38628
SCARD skipped               0
ZCARD inflight             228
ZCARD retry_scheduled       73

visited ∩ failed_permanent   7365   <- impossible under current-code-only CAS semantics
visited - known               7457   <- legacy-schema visited entries never added to `known`
failed_permanent - known         0   <- failed_permanent has always gone through the known-gated path
skipped - known                   0
```

Legacy/residual keys still present in the namespace (would not exist if
`frontier.clear()` had ever actually run):

- `crawler:urls:queued` — legacy SET, 99 members. Current schema has no such
  key (`queued` is a derived count, not a stored set —
  `RedisURLFrontier.get_status_counts()`).
- `crawler:urls:metadata:{url}` — legacy per-URL metadata hashes, **80,224
  keys**, superseded by the current `meta:{url}` hash schema
  (`core/redis_frontier.py:175-179`).
- `crawler:urls:failed` — confirmed **absent**. The original schema wrote
  failure outcomes only to SQLite (`self.url_database.update_status(cleaned,
  "failed")`), never to Redis. This is exactly why `failed_permanent - known
  = 0` while `visited - known = 7457`: `failed_permanent` has only ever been
  written by the current, `known`-gated pipeline, while legacy `visited`
  writes bypassed it entirely.

## 3. Is `--clear-db` sufficient? No.

Correct/complete for the SQLite backend. Structurally incomplete for the
Redis backend: it clears zero Redis keys, every time, regardless of which
backend is configured.

## 4. Is current frontier logic (`core/redis_frontier.py`) implicated? No.

Traced every write path in the current Lua scripts:

- `finalize_terminal()` (the only place `visited`/`skipped`/`failed_permanent`
  are ever `SADD`ed) is reachable only from `_complete_claim_script`'s
  token-CAS'd completion.
- `_complete_claim_script` is reachable only for a URL that has an active
  `claim:{url}` hash, which is only created by `_claim_next_script`.
- `_claim_next_script` only pops URLs that exist in a domain queue, which is
  only populated by `_add_url_script`, which does `SADD known` before
  anything else.

So `failed_permanent ⊆ known` is structurally guaranteed by current code
alone, and `visited ∩ failed_permanent = ∅` is guaranteed by the
token-CAS'd, exactly-once completion per claim. Both properties are
confirmed empirically for `failed_permanent` (`failed_permanent - known =
0`). The violation is entirely attributable to pre-existing legacy data
sharing current key names, not to a bug in the current claim/completion
state machine.

## 5. The 228 inflight / 73 retry_scheduled entries

Both look like legitimate current-schema state, consistent with the reported
crash (VS Code process/terminal died mid-run): matching `crawler:claim:*` and
`crawler:attempts:*` hash keys are present. `RedisURLFrontier.reclaim_and_promote()`
— the method that sweeps abandoned inflight leases and promotes due retries —
is implemented but, per its own class docstring
(`core/redis_frontier.py:58-63`), **not wired to run automatically anywhere**
in `CrawlerManager`. So these 228/73 entries are simply un-reconciled since
the crash, not corrupted and not part of the invariant-violation mechanism
above.

## 6. Overall interpretation

Not "one clean run that crashed." A clean-ish current run's data
superimposed on substantial residual state from at least one earlier,
incompatible Redis schema generation, because `--clear-db` has apparently
never actually reached Redis for this namespace.

## 7. Required fix (described, not implemented)

Smallest fix: `CrawlerManager.clear_storage()` (`core/crawler_manager.py:248`)
should also clear the frontier when it's Redis-backed, e.g. call
`self.frontier.clear()` alongside the existing SQLite clears — guarded the
same way `core/crawler_manager.py:522` already guards
`reclaim_and_promote` (`hasattr(self.frontier, "clear")`, or an explicit
Redis-backend check). No change is needed or appropriate in
`tests/report_lib.py`, `tests/report.py`, or the Lua scripts in
`core/redis_frontier.py` — they correctly detected and surfaced real,
pre-existing contamination exactly as designed.

Not implemented as part of this audit. Redis state was not modified.
