# Media Evidence — Phase 1 Implementation

> See [`system-architecture.md`](system-architecture.md#16-media-evidence-architecture)
> for the current, as-built high-level summary, and
> [`media-evidence-redis-design.md`](media-evidence-redis-design.md) for
> the detailed design this phase implemented. This document is the
> implementation record: what Phase 1 actually built, including where it
> deviated from the design.

Status: **implemented**. This document records what Phase 1 actually built,
against the design in `docs/architecture/media-evidence-redis-design.md`
(referenced below by section, e.g. "§5"). Phase 1 scope: the storage/
coordination layer only (`MediaEvidenceStore`, `SQLiteMediaEvidenceStore`,
`RedisMediaEvidenceStore`) — no fingerprinting algorithms, no downloader, no
worker loop, no domain-scoring consumer. See "Next phase" at the end.

---

## 1. Files changed

**New:**

| File | Purpose |
|---|---|
| `storage/media_evidence_store.py` | `MediaEvidenceStore` Protocol, `FingerprintJob`/`FingerprintResult` dataclasses, job/decision status constants, `compute_discovery_id`, `derive_asset_status`, validation/truncation helpers. Backend-agnostic — no Redis or sqlite3 import. |
| `storage/sqlite_media_evidence_store.py` | `SQLiteMediaEvidenceStore` — rename + hardening of the old `MediaEvidenceDatabase`. Development/testing backend. |
| `storage/redis_media_evidence_store.py` | `RedisMediaEvidenceStore` — the production, fleet-wide backend. Six Lua scripts (§5 below). |
| `tests/redis_media_evidence_store_test.py` | Unit/CAS/lease/retry/event tests against a real local Redis (skips if unavailable). |
| `tests/media_evidence_multiprocess_test.py` | Independent-OS-process claim-safety test, 2/4/8 workers. |
| `tests/benchmarks/media_evidence_benchmark.py` | Small deterministic smoke benchmark (not pytest-collected, matches the `tests/benchmarks/` convention). |

**Modified:**

| File | Change |
|---|---|
| `storage/media_evidence_database.py` | Reduced to a one-line backward-compat alias: `MediaEvidenceDatabase = SQLiteMediaEvidenceStore`. Nothing in this repo imports from here anymore. |
| `core/config.py` | New `MediaEvidenceConfig` (backend type, Redis connection/namespace, retention caps, lease/retry tuning); added as `CrawlerConfig.media_evidence`. |
| `config.yaml` | New `crawler.media_evidence` block, active backend set to `redis` (mirrors the existing `frontier` block's convention of shipping with `redis` active and `sqlite` commented above it). |
| `core/crawler_manager.py` | New module-level `build_media_evidence_store(config)`; `CrawlerManager.media_database` now built from it. No fallback on Redis failure (see §8). |
| `main.py` | `--claim-sample-job`/`--mark-match` replaced with `--claim-fingerprint-job`/`--complete-fingerprint-job` (+ `--claim-token`, `--decision`, `--media-backend`) against the new API. |
| `tests/media_evidence_test.py`, `tests/fingerprinter_queue_test.py`, `tests/streaming_manifest_test.py` | Updated for the renamed store/methods; `fingerprinter_queue_test.py` gained CAS/retry coverage and a decoupling test (§9 below) replacing the old direct-`DomainDatabase`-coupling test. |

No other file needed changes: every crawler engine (`crawler/*.py`) already treats `media_database` as duck-typed (`record_media_link`/`record_manifest_variants` only), so `core/crawler_manager.py` was the only production call site to update.

---

## 2. Interface (`storage/media_evidence_store.py`)

```python
record_media_link(*, url, source_page=None, referrer_url=None, discovered_by="unknown",
                   discovery_method="parser", media_type=None, mime_type=None,
                   content_length=None, priority=10) -> str
record_manifest_variants(asset_id: str, variants: list[dict]) -> None
list_media_assets(*, limit: int | None = None) -> list[dict]
list_observations(asset_id: str) -> list[dict]
list_manifest_variants(asset_id: str) -> list[dict]
get_fingerprint_jobs(statuses: Sequence[str] | None = None) -> list[dict]
claim_next_fingerprint_job(worker_id: str) -> FingerprintJob | None
renew_job_lease(asset_id: str, token: str) -> bool
complete_fingerprint_job(asset_id: str, token: str, *, result: FingerprintResult) -> bool
fail_fingerprint_job(asset_id: str, token: str, *, error_class: str, last_error: str, retryable: bool) -> bool
reclaim_expired_jobs(batch_size: int = 200) -> tuple[int, int]
get_status_counts() -> dict[str, int]
clear() -> None
close() -> None
```

Deviations from the design doc's §17 sketch, both minor and made for
consistency/simplicity rather than a contradiction with the architecture:

- **`get_sample_jobs` → `get_fingerprint_jobs`.** The design doc's §17 keeps
  the old SQLite-era name; every other method already uses the
  `fingerprint_job` vocabulary the doc establishes elsewhere (§5, §12, §25).
  Renamed for internal consistency — same behavioral role (introspection/
  CLI listing), not a semantic change.
- **`mark_asset_matched` dropped entirely.** §17 keeps a token-guarded
  version of it; this implementation folds "confirmed" into
  `complete_fingerprint_job`'s `FingerprintResult.aggregate_decision` instead,
  since §2 already classifies "confirmed match" as a *value*, not a
  separate entity or operation ("it needs an event more than it needs its
  own storage record"). A second method that does almost the same thing as
  `complete_fingerprint_job` would have been the redundant API the brief
  warns against.
- **`asset_id` is `str` on both backends.** SQLite's underlying identity is
  still an autoincrement integer rowid (§21/§23: no SQLite redesign), but
  the Protocol exposes it as `str(rowid)` so callers never branch on
  backend type. Redis's `asset_id` is always the `discovery_id` hex digest
  (§3) — a string identity was already required there.

`FingerprintResult.__post_init__` validates `aggregate_decision` against
`{"confirmed", "rejected", "uncertain"}` — an explicit domain exception
(`ValueError` subclass semantics via dataclass validation) rather than a
free-form string reaching either backend, closing bug #6 from the design
doc's §1 audit ("`fingerprint_status` is a free-form string, not a
validated enum").

---

## 3. Asset status: derived, not stored (closes a confirmed SQLite bug)

Per §1a finding (b), the old `media_assets.status`/`sample_jobs.status`
columns could desync because two independently-writable fields protected
different, inconsistent terminal-state sets. Both new backends implement
§2's recommended fix structurally: **there is no `status` column/field on
the asset at all.** `derive_asset_status(job_status, aggregate_decision)`
(pure function, `storage/media_evidence_store.py`) computes it at read
time:

- job `queued` → asset `queued_for_fingerprint`
- job `claimed`/`retry_scheduled`/`permanent_failure` → same token
- job `completed` → the `FingerprintResult.aggregate_decision` value
  (`confirmed`/`rejected`/`uncertain`)

This makes the desync bug structurally impossible rather than merely
avoided by convention — `record_media_link`/rediscovery never writes to
this field under any code path, on either backend.

**Resolved ambiguity** (§5a flagged this open): whether a `permanent_failure`
asset should be reopened by rediscovery. Implemented as **no** (§5a's own
working recommendation) — `record_media_link`'s job-upsert branch never
resets `status`, `retry_count`, or anything else once a job exists,
regardless of its current state, including `permanent_failure`. An
administrative re-queue path is not implemented in Phase 1 (no operational
need yet — flagged as a Phase 2 candidate, not silently built).

---

## 4. SQLite backend (`SQLiteMediaEvidenceStore`)

Tables: `media_assets`, `media_observations` (unbounded — §4's cap is a
Redis-specific mitigation, not preserved SQLite behavior), `fingerprint_jobs`
(replaces `sample_jobs`; states `queued`/`claimed`/`completed`/
`retry_scheduled`/`permanent_failure` — the dead `sampled`/`hashed` states
from §5a are not migrated), `fingerprint_results` (new — separates durable
evidence from operational job state per §2), `manifest_variants` (unchanged
shape, unbounded — dev/test scope only).

Claim/renew/complete/fail use a `threading.Lock` plus an explicit
`claim_token` column checked before every mutation — enough to make
in-process concurrent callers safe (the only concurrency this backend needs
to support per §22, which explicitly scopes multi-process distributed-claim
testing to Redis only) and closes bugs #1/#2 from the design doc's audit
("any caller can complete any job").

`clean_media_url` continues to define the dedup key (`UNIQUE(url)`); no
`discovery_id`/sha256 hashing is used here — SQLite's own autoincrement
identity is already deterministic-enough for a single-writer file, and §21
doesn't require SQLite to adopt Redis's identity scheme.

---

## 5. Redis backend (`RedisMediaEvidenceStore`)

### Keyspace (`{ns}` = `media_evidence.redis_namespace`, default `evidence`; `{aid}` = `discovery_id`)

| Key | Type | Purpose |
|---|---|---|
| `{ns}:asset:{aid}` | HASH | Asset identity/description (canonical_url, media_type, source_domain, mime_type, first_seen, last_seen, observation_count, last_source_page/referrer_url/discovered_by/discovery_method, content_id slot) |
| `{ns}:asset:{aid}:observations` | LIST | Capped ring buffer (LPUSH+LTRIM), most recent `max_observations_per_asset` |
| `{ns}:asset:{aid}:variants` | HASH | `variant_url -> JSON{bandwidth,resolution,codecs,discovered_at}`, capped count |
| `{ns}:assets:all` | ZSET | `aid -> last_seen` — **addition beyond §12's table**, needed because `list_media_assets` has no other way to enumerate assets without a keyspace `SCAN`; not in the original design doc |
| `{ns}:jobs:queue` | ZSET | `aid -> priority*1e6 + seq` — global priority queue |
| `{ns}:jobs:seq` | STRING | Monotonic counter, tiebreaks queue score |
| `{ns}:job:{aid}` | HASH | status, priority, retry_count, error_class, last_error, claimed_by, created_at, updated_at |
| `{ns}:jobs:claim:{aid}` | HASH | token, worker_id, claimed_at — CAS ownership record |
| `{ns}:jobs:inflight` | ZSET | `aid -> lease_expiry_epoch` |
| `{ns}:jobs:retry_scheduled` | ZSET | `aid -> eligible_retry_epoch` |
| `{ns}:jobs:permanent_failure` | SET | terminal failures |
| `{ns}:jobs:completed_total` | STRING | counter, `INCR`'d by the complete script — backs `get_status_counts()["completed"]` |
| `{ns}:result:{aid}` | HASH | durable `FingerprintResult` fields + `processed_at` (server TIME) |
| `{ns}:events:confirmed_match` | STREAM | `XADD` on `aggregate_decision == "confirmed"`, `XTRIM ~ confirmed_match_stream_maxlen` |

All timestamps (`first_seen`, `last_seen`, `observed_at`, job `created_at`/
`updated_at`, lease/retry scores, `result:*:processed_at`) come from Redis
`TIME` inside the Lua scripts, never client clocks — per the brief's
requirement to use server time for distributed timestamps. (SQLite, having
no distributed-clock problem, keeps its existing `datetime.now(UTC)`
convention.)

### Lua scripts (six total, matching §24's count)

1. **`record_media_link`** — one round trip: upsert asset fields (COALESCE-
   style: new value wins only if non-empty, mirroring the old SQL), always
   bump `assets:all`/`observation_count`, `LPUSH`+`LTRIM` one observation,
   and — iff `{ns}:job:{aid}` doesn't exist yet — create it and `ZADD` the
   queue; otherwise only ratchet `priority` down (`MIN`) and re-`ZADD` the
   queue *only if* the job is still `queued` (never touches a
   claimed/retry/terminal job's status or position). **Invariant:** exactly
   one job is ever created per asset, for the asset's entire lifetime.
2. **`record_manifest_variants`** — one round trip regardless of variant
   count (`cjson.decode` of a JSON array passed as one ARGV); once
   `max_variants_per_asset` distinct `variant_url`s exist, new ones are
   dropped, but updates to already-known variants still apply.
3. **`claim_next`** — `ZRANGE`+`ZREM` the queue head (indivisible within the
   script — exactly one caller can ever receive a given `aid`), writes the
   claim hash + `jobs:inflight` entry, reads back `canonical_url`/
   `media_type` for the caller. **Invariant:** a given `aid` is popped from
   `jobs:queue` at most once between being added and being claimed.
4. **`renew_lease`** — token CAS (`HGET` claim token, compare) + conditional
   `ZADD` of a fresh `inflight` score, one round trip.
5. **`complete`** — token CAS, then (all in the same script): clear
   claim/inflight, mark the job `completed`, `INCR` the completed counter,
   write every present `FingerprintResult` field generically (`pairs()`
   over the decoded JSON, so the script needs no per-field knowledge of the
   result shape), and — iff `aggregate_decision == "confirmed"` — `XADD`+
   `XTRIM` the confirmed_match stream. **Invariant:** either the entire
   transition applies, or (CAS mismatch) none of it does — a stale
   worker's completion can never partially land.
6. **`fail`** — token CAS, then the generic backoff/permanent-failure state
   machine (§8): `retryable and retry_count < max_retries` → `retry_scheduled`
   with `base_backoff * 2^(retry_count-1)` capped at `max_backoff`;
   otherwise → `permanent_failure`. Retryability is always the caller's
   classification, never inferred from `error_class`/`last_error` text.
7. **`reclaim_and_promote`** *(the seventh script — §24 said "six" before
   implementation; `record_manifest_variants` wasn't separately counted
   there, so the actual count is seven, noted here rather than silently
   left inconsistent)* — two bounded-batch phases in one round trip,
   directly mirroring `core/redis_frontier.py`'s `reclaim_and_promote`:
   (a) `ZRANGEBYSCORE jobs:inflight -inf now LIMIT 0 batch_size` → each
   expired lease is treated as a recoverable crash (§8: "always retryable,
   no reported error") and pushed through the same retry/permanent-failure
   decision as `fail`; (b) `ZRANGEBYSCORE jobs:retry_scheduled -inf now` →
   promoted back to `jobs:queue` with a fresh `seq`. Idempotent by
   construction (each phase only acts on members it first `ZREM`s from the
   source structure), so it's safe to run redundantly/concurrently from
   every worker with no leader election, exactly like the frontier's own
   sweep.

Every script documents its own atomicity/invariant in an inline comment
(coding standard requirement #18) rather than only here.

### A subtlety found while testing: inclusive vs. exclusive "due" comparison

Redis's `reclaim_and_promote` computes one `now` (via `TIME`) and reuses it
for **both** phases within the same script call. Because `ZRANGEBYSCORE`'s
range is inclusive, a lease reclaimed in phase (a) with `base_backoff=0`
(score = `now + 0 = now`) is *immediately* visible to phase (b)'s
`ZRANGEBYSCORE -inf now` in the same call — one call both reclaims and
requeues it. SQLite's equivalent two-query implementation uses a strict
`<` comparison for the same check, so the analogous same-call promotion
does **not** happen there with a zero backoff; a second sweep is needed.
This is a harmless, only-visible-at-`backoff=0` (an unrealistic production
value) difference between the two independent backends, not a correctness
issue — documented here because it was surprising enough during testing to
be worth recording rather than leaving as an unexplained test quirk.

---

## 6. Claim/lease semantics

Identical shape to the frontier (`core/redis_frontier.py`): a fresh
`uuid4().hex` token per claim, stored in a CAS record, checked by every
subsequent `renew`/`complete`/`fail`. `fingerprint_lease_ttl` defaults to
900s (§7's initial default — an order of magnitude longer than the
frontier's 90s, since fingerprinting is minutes not milliseconds).
`fingerprint_heartbeat_interval` defaults to `None`, meaning callers should
derive it via the existing `core.claim_heartbeat.default_heartbeat_interval`
(lease/3) — this implementation does not duplicate that logic (per the
brief's explicit instruction not to), it only exposes the config knob and
documents the intended reuse; wiring an actual heartbeat loop belongs to
the fingerprinter worker (Phase 2 — no such worker exists yet to wire it
into).

---

## 7. Retry/recovery semantics

`retryable` is always supplied by the caller to `fail_fingerprint_job`;
neither backend infers it from `error_class`/`last_error` text (closes bug
#6). Mechanics are identical on both backends: `retry_count` increments on
every retry transition, backoff = `base_backoff * 2^(retry_count-1)` capped
at `max_backoff`, `retry_count >= max_retries` → `permanent_failure`
(absorbing state, never auto-reopened — §3 above). A crashed worker (lease
expiry, no reported error) is treated identically to a reported retryable
failure, per §8's "worker crash — always recoverable" row.
`max_retries` defaults to 2 (§8's suggested value, smaller than the
frontier's 3, given fingerprinting's higher per-attempt cost).

---

## 8. Configuration

`core/config.py`'s new `MediaEvidenceConfig` (under `CrawlerConfig.media_evidence`):

```yaml
crawler:
  media_evidence:
    type: "redis"                       # or "sqlite"
    redis_host: "localhost"
    redis_port: 6379
    redis_namespace: "evidence"          # separate from frontier's "crawler"
    max_observations_per_asset: 20       # §4 default
    max_variants_per_asset: 20           # §10 default
    fingerprint_lease_ttl: 900.0         # §7 default
    fingerprint_heartbeat_interval: null # auto-derives lease/3
    max_retries: 2
    base_backoff: 5.0
    max_backoff: 300.0
    reclaim_batch_size: 200              # Redis-only
    confirmed_match_stream_maxlen: 10000 # Redis-only
```

`storage.media_sqlite_path`/`storage.enable_media_evidence` (pre-existing
fields) are unchanged in meaning: the former is still the SQLite file path,
the latter still the overall on/off toggle. `media_evidence.type` only
selects which backend is used when evidence is enabled.

**Backend selection has no fallback, by design** (`core/crawler_manager.py`'s
`build_media_evidence_store`): if `type: redis` and Redis is unreachable,
construction raises and `CrawlerManager.__init__` fails — verified directly
(see §10, "Test coverage"). This is a deliberate divergence from the
frontier's own `type: redis` fallback-to-SQLite behavior in the same file,
per the brief's explicit instruction: a production media-evidence Redis
outage must be visible, not silently degraded to a different backend. The
frontier's own fallback semantics were left untouched (not in scope here).

`main.py` gained `--media-backend {sqlite,redis}`, closing the CLI-flag gap
§21 flagged ("the frontier itself never closed this gap") — used by the new
`--claim-fingerprint-job`/`--complete-fingerprint-job` CLI stub, which now
requires an explicit `--claim-token` to complete a job (reflecting the new
CAS requirement; the old `--mark-match` could complete any asset id with no
claim at all, which is exactly bug #2).

---

## 9. Confirmed-match event (evidence-layer side only)

`complete_fingerprint_job` on the Redis backend, when
`aggregate_decision == "confirmed"`, `XADD`s
`{ns}:events:confirmed_match` (`asset_id`, `source_domain`, `matched_title`,
`confidence`, `processed_at`) and trims the stream to
`confirmed_match_stream_maxlen`. No consumer is implemented — per the
brief, the domain-scoring consumer is explicitly out of scope for Phase 1.
`RedisMediaEvidenceStore.read_confirmed_match_events()` is a small,
non-Protocol test/ops helper (not part of `MediaEvidenceStore`) that exists
only so the event side of `complete_fingerprint_job` is verifiable without
a real consumer (see `TestConfirmedMatchEvent` in the Redis test file).

The SQLite backend has no equivalent — it never talks to `DomainDatabase`
either (verified by `test_completing_a_job_does_not_touch_domain_database`
in `tests/fingerprinter_queue_test.py`), consistent with §19's requirement
that the evidence layer never import or know about `DomainDatabase`
directly, on either backend.

---

## 10. Test coverage

```
tests/media_evidence_test.py               3 tests  (SQLite: dedup, rediscovery-doesn't-duplicate-job, HTML link extraction — unrelated, kept)
tests/fingerprinter_queue_test.py           5 tests  (SQLite: claim/complete, DomainDatabase decoupling, stale-token rejection, retry->permanent, non-retryable->permanent)
tests/streaming_manifest_test.py            3 tests  (manifest parsing + SQLite variant storage)
tests/redis_media_evidence_store_test.py   18 tests  (asset/observation/variant behavior, claim/CAS, lease/recovery, fail/retry, confirmed-match event, status counts)
tests/media_evidence_multiprocess_test.py   3 tests  (2/4/8 independent OS processes, asserts duplicate_claims == 0 each time)
```

All pass locally (`python -m pytest tests/ -q`, 184 passed / 2 skipped —
skips are pre-existing browser-crawler tests gated behind
`RUN_BROWSER_CRAWLER_TESTS=1`, unrelated to this work). The full suite also
directly verifies, outside pytest, the two properties called out above as
load-bearing:

- Redis-unreachable + `type: redis` → `CrawlerManager()` raises
  `redis.ConnectionError` (not a silent SQLite fallback).
- SQLite-backed `CrawlerManager()` end-to-end: `record_media_link` → asset
  visible via `media_database`.

**Pre-existing, unrelated flakiness noted during this work:**
`tests/redis_frontier_test.py::TestMultiWorkerCoordination::test_get_next_url_no_duplicates`
intermittently fails on repeated runs (reproduced independently of any
change in this phase — different namespace, different file, frontier code
untouched). Not investigated or fixed here; out of scope for Media
Evidence Phase 1.

### Smoke benchmark

`python tests/benchmarks/media_evidence_benchmark.py --assets 500 --claim-workers 4`,
against local Redis, single run:

| Metric | Value |
|---|---|
| Insert throughput | ~2,290 `record_media_link`/s (p50 0.36 ms, p90 0.42 ms, p99 0.61 ms) |
| Claim throughput | ~7,260 claims/s across 4 threads (p50 0.19 ms, p90 0.56 ms, p99 1.20 ms) |
| Complete throughput | ~7,260 completions/s (p50 0.15 ms, p90 0.47 ms, p99 1.30 ms) |
| Duplicate claims | 0 |
| Redis memory growth | ~905 KB for 500 assets (~1.8 KB/asset — consistent with §15's ~1-2 KB/asset ballpark) |

Deliberately small and single-machine (localhost, no network latency) — a
correctness/sanity smoke test, not a capacity-planning result. A larger,
multi-host benchmark campaign is explicitly deferred (§17 of the brief:
"a larger benchmark campaign can be a subsequent phase").

---

## 11. Known limitations / deferred decisions

- **`content_id` linking (§3) is not implemented.** The `content_id` slot
  exists on the Redis asset hash (nullable, always empty in Phase 1) and
  in the design's data model, but no code populates it or maintains the
  `{ns}:content:{content_id}:assets` reverse index — there's no
  fingerprinter yet to produce a content identity, and building the
  linking mechanism speculatively would be exactly the kind of
  unjustified entity the brief warns against.
- **Per-source-domain new-asset-creation circuit breaker (§16) is not
  implemented.** Explicitly flagged in the design doc's Appendix as an
  open product question, not a Phase 1 requirement.
- **No administrative re-queue for `permanent_failure`.** §5a's
  no-auto-reopen recommendation is implemented; the explicit re-queue
  operation it says should exist as the only way back is not — no
  operational tooling exists yet that would call it.
- **`get_fingerprint_jobs()` cannot list `completed` jobs on Redis.** By
  design (§12: job hashes are discardable operational state after
  completion; durable evidence lives in `result:{aid}`), there is no
  bounded index of completed asset ids. `get_status_counts()["completed"]`
  gives the exact count via a counter; `list_media_assets()` gives the
  per-asset evidence.
- **`fingerprint_heartbeat_interval` is a config value only** — no
  heartbeat loop exists in this phase (nothing calls `renew_job_lease` on
  a timer), because there is no fingerprinter worker yet to run one. The
  config knob and its clamp-below-lease relationship (matching
  `core.claim_heartbeat.resolve_heartbeat_interval`) are ready for Phase 2
  to use directly.
- **Redis AOF/RDB persistence tuning (§14)** is an operational/deployment
  concern, not application code — this phase does not configure or verify
  the recommended `appendfsync`/`save` settings on any real Redis
  instance; that belongs to deployment, not this repository.

---

## 12. Recommended Phase 2

Per the brief's strict stop condition, Phase 1 stops here. Suggested next
phase (not started, not designed in detail): the `fingerprinter/` package
sketched in §23 — a worker loop that calls
`claim_next_fingerprint_job`/`renew_job_lease` (wired to
`run_with_heartbeat`, reusing `core/claim_heartbeat.py` unmodified) /
`complete_fingerprint_job`/`fail_fingerprint_job` around real media
download + DINOv2/pHash/audio/temporal-verification algorithms, followed
by a decoupled consumer of `{ns}:events:confirmed_match` that applies the
domain-score feedback `main.py --complete-fingerprint-job` explicitly does
*not* do in this phase.
