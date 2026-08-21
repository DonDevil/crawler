# Phase 3 — Target Registration and Scoping

## Status: COMPLETE (this phase's own scope) — the crawler→fingerprinter bridge remains NOT implemented

## 1. Problem

`docs/architecture/phase-3-crawler-fingerprinter-bridge.md` (the prior Phase
3 audit) found the crawler↔fingerprinter bridge mechanically feasible but
BLOCKED: the fingerprinter's `FingerprintCandidate`/`Job` contract requires
non-empty `target_id`/`target_version` on every job (which registered
movie/content a candidate is being checked against), and the crawler had no
legitimate source for either value — no catalog, no watchlist, nothing.
Fabricating a value was explicitly ruled out (it would pass validation but
guarantee every job dies inside a worker as a confusing `KeyError`-derived
`PERMANENT_ERROR`, after paying DINOv2's ~95%-of-latency inference cost).

## 2. Existing blocker (recap)

- `integration/candidate.py::FingerprintCandidate.validate()` and
  `work_queue/jobs.py::Job.from_stream_fields()` (fingerprinter repo) both
  reject an empty `target_id`/`target_version`.
- `target/registry.py::TargetRegistry.register_target(target_id,
  target_version, media_path, media_metadata)` (fingerprinter repo) is the
  **only** way a `(target_id, target_version)` pair becomes valid — an
  opaque, caller-supplied pair with no format validation and no external
  catalog lookup.
- `docs/design/design-proposal-1.md` (fingerprinter repo, line 229): *"Target
  identity: `target_id` — an identifier assigned by rights-holder/ops
  tooling, independent of any crawler asset id."* Line 273 names *"Target
  ingestion/registration workflow and API surface (who creates `target_id`s
  and uploads content)"* as an explicitly deferred, unsolved question in the
  fingerprinter's own founding design.
- Live Redis (`KEYS fingerprint:target:*`, both DB 0 production and this
  phase's isolated test DBs before this phase's changes): zero results.

## 3. Design decision

Two separate, small, additive mechanisms — no redesign of either existing
queue, no new competing registry:

**A. Target registration** (`/home/darkdevil/Desktop/anti_piracy/fingerprinter/scripts/register_target.py`,
new file, fingerprinter repo) — a thin CLI wrapper around the fingerprinter's
own, already-existing `TargetRegistry.register_target()`. Zero new
registration logic; this file adds no capability `TargetRegistry` didn't
already have, it only makes calling it a first-class, documented, ops-usable
action instead of an ad-hoc Python snippet.

**B. Target scoping** (`core/target_scope.py`, new file, crawler repo, plus
small additive changes to five existing crawler files — §8) — lets one
crawler run declare which *already-registered* target its discovered
fingerprint jobs are associated with, and validates that declaration against
the fingerprinter's own registry before any crawling starts.

**Why this split:** requirement 3 of this phase's brief ("Registration must
ultimately use the fingerprinter's existing `TargetRegistry` semantics") and
the "Source of truth" section ("avoid creating two competing target
registries") both point the same direction — the fingerprinter's Redis-backed
`TargetRegistry` is authoritative, and this repo must never re-implement or
duplicate its identity/content-hash logic. The crawler repo therefore only
ever *reads* one fact from that registry ("does this exact `(target_id,
target_version)` exist"), via the documented `fingerprint:target:{id}:
{version}` key convention (`target/keys.py::target_key`, fingerprinter
repo) — never the raw `TargetRecord` internals (`content_sha256`,
`media_metadata`, ...), and never a write.

**Why the registration script lives in the fingerprinter repo, not this
one:** `docs/design/design-proposal-1.md`'s opening paragraph explicitly
excludes "direct crawler Python imports" between the two repos (restated in
the prior Phase 3 bridge doc, §4) — the two systems must stay independently
deployable. A registration tool needs `target.registry.TargetRegistry`,
`target.cache.FilesystemEmbeddingCache`, and
`target.segment_cache.FilesystemSegmentEmbeddingCache` as actual Python
objects (not a wire protocol), so it can only live inside the fingerprinter
repo's own dependency tree.

## 4. Target registration authority

`target.registry.TargetRegistry` (fingerprinter repo) remains the sole
authoritative store — this phase does not add a second registry, does not
cache target metadata in the crawler's own Redis namespace, and does not
change `TargetRegistry`'s contract, `TargetRecord`'s shape, or any
`fingerprint:target:*` key format. `scripts/register_target.py` is a new
caller of that existing, unmodified contract, not a modification of it —
satisfying this phase's constraint 10 ("do not modify... unless the audit
proves an actual defect"; no defect was found or claimed).

Usage (fingerprinter repo):

```bash
cd /home/darkdevil/Desktop/anti_piracy/fingerprinter
.venv/bin/python3 -m scripts.register_target \
    --target-id blast --target-version v1 \
    --media-path /path/to/reference/blast.mp4 \
    --redis-url redis://localhost:6379/0 \
    --metadata-json '{"title": "Blast"}'
```

`--redis-url`/`--target-cache-path` default to the same values
`worker/main.py`'s own `REDIS_URL`/`TARGET_CACHE_PATH` conventions use, so a
target registered this way is immediately visible to a running fingerprint
worker with no extra configuration.

## 5. Target identity semantics

`target_id` is an opaque string, assigned by whoever runs the registration
script (a rights-holder or ops operator) — never derived from a URL,
filename, discovered title, crawler asset id, or `matched_title`. This
phase's own code never generates one: `core/target_scope.py`'s
`resolve_target_scope()` only ever accepts an already-decided value from
configuration/CLI, and raises rather than inventing one when the input is
incomplete (see §9).

## 6. Target version semantics

`target_version` is a second opaque, caller-assigned label — per the
fingerprinter's own `target/identity.py` docstring, *not* derived from
content (two different `target_version`s can wrap byte-identical content;
that's a separate fact `TargetRegistry.find_by_content_hash` answers,
irrelevant to this phase). A `(target_id, target_version)` pair is the whole
identity this phase cares about — `target_id` alone is never treated as
sufficient (`tests/target_scope_test.py::
TestVerifyTargetRegistered::test_multiple_targets_are_selectable_without_ambiguity`
asserts a registered `target_id` with a *different*, unregistered
`target_version` is still rejected).

## 7. Crawler target-selection semantics

**Where the scope lives (the phase brief's "critical question"):**
configuration + CLI, following this repo's existing pattern exactly —
`MediaEvidenceConfig.target_id`/`target_version` (`core/config.py`, both
`Optional[str] = None`) are the config.yaml-level surface, and `main.py`'s
new `--target-id`/`--target-version` flags override them per run,
identically to how `--media-backend` already overrides
`media_evidence.type` and `--crawler-engine` already overrides
`crawler.engine`. No new run-level object, no new CLI framework: this reuses
the config/CLI-override mechanism every other per-run choice in this repo
already goes through.

**Why not a numeric/threshold mapping, a `TargetCatalog`, or a
`--target-name` lookup:** none of those exist anywhere in either repo today
(§2), and the brief explicitly forbids inventing one ("do not fabricate...
target mappings"). A crawler run names exactly one already-registered
`(target_id, target_version)` pair directly — the smallest mechanism that
satisfies "a crawler run must have an explicit target scope" without adding
speculative machinery.

**Validation, before any crawling starts:** `build_media_evidence_store()`
(`core/crawler_manager.py`), the single choke point both the main crawl path
and `main.py`'s existing `--claim-fingerprint-job`/`--complete-fingerprint-job`
CLI stub already go through, now:

1. Calls `resolve_target_scope(target_id, target_version)` — raises
   `TargetScopeError` if exactly one of the two is set (§9).
2. If a scope resolved and the backend is Redis: calls
   `verify_target_registered(store.redis_conn, scope)` — raises
   `TargetNotRegisteredError` if the fingerprinter has no matching
   registration (§9). Reuses the *same* Redis connection the evidence store
   already opened (this deployment's crawler evidence Redis and
   fingerprinter registry Redis are the same physical instance, per the
   prior Phase 3 audit's live inspection, §7 of that document) rather than
   opening a second connection for one `EXISTS` check.
3. If a scope resolved and the backend is SQLite: raises `TargetScopeError`
   immediately (§9) — the fingerprinter's `TargetRegistry` only exists in
   Redis, so there is nothing to validate against.

`CrawlerManager.__init__` calls this before `prepare_frontier()`/`run()` are
ever reachable, so a misconfigured or unregistered target scope stops the
process before a single URL is crawled.

## 8. How target identity reaches the future bridge

Every fingerprint job created by a target-scoped crawler run carries its
run's `target_id`/`target_version` on the job itself — not a separate
run-level side-channel the bridge would have to correlate. This directly
answers the brief's "the selected target must be available to the future
bridge for each evidence job":

- `storage/media_evidence_store.py::FingerprintJob` gained two new optional
  fields, `target_id`/`target_version` (default `None`, so every existing
  call site/test constructing a `FingerprintJob` without them is
  unaffected).
- `RedisMediaEvidenceStore`/`SQLiteMediaEvidenceStore` each gained a
  constructor-level `target_scope: Optional[TargetScope] = None` parameter.
  The scope is written onto a job's hash/row **once, at job-creation time**
  (Redis: inside `_record_media_link_script`'s `is_new` branch; SQLite: the
  initial `INSERT`), and never touched again by rediscovery — mirroring the
  existing "one job per asset, for its whole lifetime" invariant both
  backends already enforce for `priority`/`media_type`. `claim_next_
  fingerprint_job()` (both backends) reads it back onto the returned
  `FingerprintJob` — the one call shape a future bridge would actually use.
- `get_fingerprint_jobs()` (Redis backend's `_build_job_dict`) also surfaces
  `target_id`/`target_version` for ops introspection — free, since the data
  already lives in the same hash a Redis backend was already reading.

**Known limitation, by design, not fixed this phase:** both evidence stores'
job model is "exactly one job per asset, for its whole lifetime" — an asset
rediscovered under a *different* crawler-run target scope keeps whatever
scope its first-ever discovery recorded. Checking one candidate against
multiple targets is not supported by the existing evidence queue's
fundamental per-asset job model; changing that would be the redesign this
phase's brief (rule 9) explicitly forbids. Documented here as a real,
pre-existing architectural constraint this phase does not attempt to solve.

## 9. Validation / failure behavior

| Condition | Behavior |
| --- | --- |
| No `target_id`/`target_version` configured at all | `resolve_target_scope(None, None)` returns `None` — unscoped run, unchanged pre-Phase-3 behavior, no implicit/default target ever substituted |
| Exactly one of `target_id`/`target_version` set | `TargetScopeError`, raised before any Redis round trip |
| Both set, but not found in the fingerprinter's `TargetRegistry` | `TargetNotRegisteredError`, raised before any crawling starts; the evidence store connection opened during the check is explicitly closed first |
| Both set, but the media evidence backend is SQLite | `TargetScopeError` — SQLite has no `TargetRegistry` to validate against, so this fails loudly rather than silently skipping validation |
| Redis unreachable during the existence check | Propagates as `redis.RedisError` (or a `redis.ConnectionError` from the store's own connection setup) — never conflated with "target not found"; an infrastructure failure and a candidate-specific rejection are kept distinct, mirroring the prior Phase 3 bridge doc's failure-classification stance |
| CLI override (`--target-id`/`--target-version`) partially set | Same `TargetScopeError` as a config.yaml-level mistake — CLI overrides flow through the exact same `resolve_target_scope`/`build_media_evidence_store` path, not a parallel check |

Explicitly out of this phase's scope (deferred, §14): whether the target's
*reference media* is still reachable/valid. This phase's `EXISTS` check only
confirms the `(target_id, target_version)` identity was registered — it does
not (and, per §7's namespace-separation reasoning, should not) read
`TargetRecord.media_path`/`content_sha256` or attempt to open the file. That
remains a fingerprinter/worker-time concern (already handled there: an
unusable target fails a job as `PermanentFailure`/`PERMANENT_ERROR`, per the
prior Phase 3 doc's failure table).

## 10. Configuration / CLI surface

```yaml
# config.yaml
crawler:
  media_evidence:
    type: "redis"
    # ... existing fields unchanged ...
    target_id: "blast"        # optional; must be set together with target_version
    target_version: "v1"      # optional; must be set together with target_id
```

```bash
python main.py --target-id blast --target-version v1 [... existing flags ...]
```

Omitting both (the default) crawls exactly as before this phase. No new
configuration section was added — both fields live directly on the existing
`MediaEvidenceConfig`, matching where they're actually consumed
(`build_media_evidence_store`), rather than introducing a standalone
`TargetConfig` for two fields.

## 11. Security / integrity considerations

- Target identity is never derived from untrusted crawler input (media
  URLs, page titles, search queries) — it only ever comes from operator-
  supplied configuration/CLI, addressing the brief's explicit concern about
  query-string-driven target inference
  (`tests/redis_media_evidence_store_test.py::TestTargetScopeAssociation::
  test_target_is_never_inferred_from_discovery_metadata` and
  `tests/target_scope_test.py::TestResolveTargetScope::
  test_scope_is_never_inferred_from_a_query_or_title_string` both assert
  this directly).
- The crawler-side existence check (`verify_target_registered`) is
  read-only (`EXISTS`) and touches no key outside the fingerprinter's own
  documented `fingerprint:target:*` convention — it never reads or writes
  `TargetRecord`'s internal fields, and never touches the crawler's own
  `evidence:*` keyspace from the fingerprinter side or vice versa.
- `scripts/register_target.py` performs no shell interpolation and no new
  subprocess calls; `--media-path` is opened directly by Python's standard
  file I/O (`target/identity.py::sha256_file`, unchanged), the same code
  path every existing fingerprinter test/benchmark that calls
  `register_target` already exercises.
- No credentials are hardcoded in either new file — Redis connection
  details are configuration/CLI-supplied in both repos, consistent with
  `worker/main.py`'s existing `REDIS_URL` convention.

## 12. Tests

**Crawler repo (new/modified):**

| File | Covers |
| --- | --- |
| `tests/target_scope_test.py` (new, 10 tests) | `resolve_target_scope` (no-scope, missing-field failures, no query/title inference), `TargetScope` construction guards, `verify_target_registered` (registered/unregistered/multiple-targets-without-ambiguity, version-mismatch rejection) |
| `tests/redis_media_evidence_store_test.py` (+4 tests, `TestTargetScopeAssociation`) | Scoped run associates target with new jobs (via `claim_next_fingerprint_job`), target appears in `get_fingerprint_jobs()` introspection, unscoped run is unaffected, target is never inferred from discovery metadata (URL/source_page/discovered_by) |
| `tests/crawler_manager_target_scope_test.py` (new, 7 tests) | Missing target_id/target_version fails clearly, unregistered target fails clearly, SQLite backend + target scope fails clearly, registered target is associated with a real `CrawlerManager`, CLI-level override is validated identically to config, no-scope run is unaffected |
| `tests/media_evidence_test.py` (+2 tests) | SQLite-backend parity: scoped/unscoped job creation behaves identically to the Redis backend |

**Fingerprinter repo:** `scripts/register_target.py` was exercised directly
(not via a new pytest file, to keep this phase's footprint in that repo to
the single new script) — see §13 for the actual command and output.

## 13. Results

Crawler repo, focused suite:

```text
$ env/bin/python3 -m pytest -q tests/target_scope_test.py tests/redis_media_evidence_store_test.py \
    tests/crawler_manager_target_scope_test.py tests/media_evidence_test.py \
    tests/media_evidence_multiprocess_test.py tests/fingerprinter_queue_test.py \
    tests/hybrid_crawler_test.py tests/crawler_manager_recovery_test.py \
    tests/crawler_manager_seed_failure_semantics_test.py
78 passed in 24.24s
```

Crawler repo, full suite:

```text
$ env/bin/python3 -m pytest -q tests/
1 failed, 352 passed, 2 skipped in 76.10s
FAILED tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::
    test_concurrent_redis_calls_use_a_bounded_shared_thread_pool
```

That failure is the **same pre-existing, timing-sensitive flake** already
documented in `docs/architecture/phase-2-crawler-fingerprint-job-trace.md`
§11 — in the URL frontier's thread-pool-offload test, nothing this phase (or
Phase 2) touched. Reproduced standalone (passes in isolation, immediately
after the full-suite failure, with no code change in between):

```text
$ env/bin/python3 -m pytest -q tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool
1 passed in 0.16s
```

Confirming it's load/timing-sensitive (fails only under full-suite
concurrency pressure), not a regression from this phase's changes — this
phase touched no frontier code, no `frontier_executor.py`, no
`redis_frontier.py`.

**Real, end-to-end proof** (not a mock): the fingerprinter's actual
`scripts/register_target.py`, its actual `TargetRegistry`, and the
crawler's actual `CrawlerManager`/`RedisMediaEvidenceStore`, against a real
local Redis (isolated test DB 1, never DB 0/production):

```text
$ cd fingerprinter && .venv/bin/python3 -m scripts.register_target \
    --target-id blast --target-version v1 --media-path /tmp/blast_reference.bin \
    --redis-url redis://localhost:6379/1 --target-cache-path /tmp/fp_target_cache_demo \
    --metadata-json '{"title": "Blast"}'
{
  "content_sha256": "667725e4470c36eaad2fe95b58773e86b7e7cc4e5f2e1f89e9f77da1a91199f7",
  "target_id": "blast",
  "target_version": "v1",
  ...
}

$ cd crawler && env/bin/python3 -c "
... CrawlerManager(config=..., target_id='blast', target_version='v1') ...
... record_media_link(url='https://cdn.example/blast-full-movie.mp4', ...) ...
... claim_next_fingerprint_job('demo-worker') ...
"
Target scope resolved and validated: TargetScope(target_id='blast', target_version='v1')
Claimed job: FingerprintJob(..., target_id='blast', target_version='v1')
SUCCESS: evidence job carries the registered target scope, ready for a future bridge to read.
```

All test-DB keys (`fingerprint:target:blast:v1`, its content-hash index
entry, and the `demo_blast_e2e:*` evidence namespace) were deleted after
this run; DB 0/production was never touched by this phase.

## 14. Limitations

- **The crawler→fingerprinter bridge is still not implemented** — this
  phase only removes the target-identity blocker the prior Phase 3 audit
  identified. Nothing here reads `evidence:jobs:queue`, calls
  `FingerprintJobSubmitter`, or writes to `fingerprint:jobs:stream:*`.
- **One target per crawler run, fixed at job-creation time** (§8) — a
  fleet checking many titles simultaneously would need either multiple
  crawler-run invocations (one per target, today's supported shape) or a
  future schema change allowing multiple jobs per asset, which is out of
  this phase's scope.
- **The crawler-side existence check is a single `EXISTS`**, not a deep
  health check — it does not confirm the target's reference media file is
  still reachable, uncorrupted, or that an embedding has ever been
  successfully built for it. That remains the fingerprinter worker's
  concern at claim time.
- **Reuses the evidence store's Redis connection for the registry check**
  (§7) — correct for this deployment (both are confirmed the same physical
  Redis instance, prior Phase 3 audit §7), but would need revisiting if a
  future deployment splits crawler-evidence Redis and fingerprinter-registry
  Redis onto different instances.
- **No CLI/tooling was added for listing already-registered targets** —
  an operator must know the exact `(target_id, target_version)` to scope a
  run to; discovering what's already registered means querying
  `TargetRegistry`/Redis directly. Not built here since the brief asks for
  the smallest mechanism, and no demonstrated need for a listing UI exists
  yet.

## 15. Explicitly deferred bridge work

Unchanged from the prior Phase 3 bridge audit's own deferred-work list,
still entirely unstarted:

1. The crawler→fingerprinter bridge itself: claiming `evidence:jobs:queue`
   jobs, mapping them (now including a real `target_id`/`target_version`)
   onto `FingerprintCandidate`, calling `FingerprintJobSubmitter.submit()`,
   and handling the delivery-semantics/recovery/duplicate-suppression design
   already worked out in `docs/architecture/
   phase-3-crawler-fingerprinter-bridge.md` §6.
2. Bridge configuration, observability, and its own deterministic/real-Redis
   test suite.
3. The controlled end-to-end smoke test proving a fingerprinter worker
   claims a bridge-forwarded job (this phase's own end-to-end proof, §13,
   stops at "the evidence job carries the right target" — it does not reach
   `fingerprint:jobs:stream:default` or a running `worker.fingerprint_worker
   .Worker` at all, by design: STOP CONDITION, do not implement the bridge
   in this phase).
4. A target-listing/discovery tool, if ops demand for one surfaces (§14).
