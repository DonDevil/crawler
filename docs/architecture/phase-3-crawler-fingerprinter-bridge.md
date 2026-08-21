# Phase 3 — Crawler → Fingerprinter Integration Bridge

## Status: BLOCKED (audit complete, implementation not started)

## 1. Objective

Connect the crawler's existing, working fingerprint-job producer
(`evidence:jobs:queue`, a Redis ZSET) to the fingerprinter's existing,
working fingerprint-job consumer (`fingerprint:jobs:stream:default`, a
Redis Stream), via a small adapter component, without redesigning or
modifying either system. Phase 2
(`docs/architecture/phase-2-crawler-fingerprint-job-trace.md`) established
that both halves work correctly in isolation and that no such adapter
exists yet.

## 2. Audit method

Read-only inspection of both repositories' actual source (not just their
docs), cross-checked against live Redis state (`localhost:6379`, DB 0, the
one shared instance both systems currently point at). No files in
`/home/darkdevil/Desktop/anti_piracy/fingerprinter` were modified. No files
in this repo were modified either — this phase produced only this document.

**Crawler side inspected:** `storage/redis_media_evidence_store.py`,
`storage/media_evidence_store.py`, `core/media_evidence_executor.py`,
`core/config.py`, `config.yaml`, `docs/architecture/media-evidence-redis-
design.md`, `docs/architecture/phase-2-crawler-fingerprint-job-trace.md`,
`tests/fingerprinter_queue_test.py`, `main.py` (CLI surface), full-repo grep
for `target_id`/`target_version`/`watchlist`/`catalog`/`movie_id`/
`content_id`/`title_id`.

**Fingerprinter side inspected:** `integration/submission.py`,
`integration/candidate.py`, `integration/idempotency.py`,
`integration/keys.py`, `integration/backpressure.py`, `integration/
outcome.py`, `integration/timing.py`, `work_queue/jobs.py`, `work_queue/
producer.py`, `work_queue/keys.py`, `worker/main.py`, `target/registry.py`,
`target/keys.py`, `docs/design/design-proposal-1.md`, `docs/architecture/
phase-12-crawler-fingerprinter-integration.md`. Live Redis: `KEYS
fingerprint:target:*` against the shared instance — zero results (no
target has ever been registered anywhere, in any environment reachable
from this audit).

Documentation and implementation agreed on every point checked — no
discrepancy to report on the contract mechanics themselves (see §3-§6).
The blocking finding (§7) is a genuine data-availability gap, not a
documentation/implementation mismatch.

## 3. Existing crawler contract (source side) — CURRENT IMPLEMENTATION

- **Queue key:** `evidence:jobs:queue` (namespace `evidence`, configurable
  via `config.yaml`'s `media_evidence.redis_namespace`; DB 0 in
  production, DB 1 in tests).
- **Structure:** Redis ZSET. Score = `priority * 1_000_000 + seq` (lower
  score claims first; `priority` is a plain `int`, lower = more urgent,
  conventional default `10`; `seq` is a monotonic `INCR` counter that
  breaks ties in insertion order).
- **Job identity:** `asset_id` = `sha256(clean_media_url(url))`
  (`storage/media_evidence_store.py::compute_discovery_id`) — one job per
  distinct canonical media URL, for the asset's whole lifetime (rediscovery
  never creates a second job).
- **Claim semantics:** `claim_next_fingerprint_job(worker_id)` — single Lua
  script, `ZRANGE`+`ZREM` the head, writes `evidence:jobs:claim:{aid}`
  (`token`, `worker_id`, `claimed_at`) and adds to `evidence:jobs:inflight`
  (a ZSET scored by lease expiry). Returns a `FingerprintJob(asset_id,
  token, canonical_url, media_type, priority, retry_count,
  lease_expires_at)`. `token` (a `uuid4().hex` minted by the *store*, not
  the caller) is the sole proof of ownership for every subsequent call.
- **Lease semantics:** `fingerprint_lease_ttl` (default 900s). `renew_job_
  lease(asset_id, token)` extends it (CAS on `token`). A worker that never
  renews or completes has its job reclaimed by `reclaim_expired_jobs()`.
- **Retry semantics:** `fail_fingerprint_job(asset_id, token, error_class,
  last_error, retryable)` — CAS on `token`; if `retryable` and `retry_count
  < max_retries` (default 2), exponential backoff (`base_backoff=5.0`,
  capped `max_backoff=300.0`) via `evidence:jobs:retry_scheduled` (a ZSET
  scored by due-time), else `permanent_failure` (a Set,
  `evidence:jobs:permanent_failure`).
- **Completion semantics:** `complete_fingerprint_job(asset_id, token,
  result: FingerprintResult)` — CAS on `token`; writes `evidence:result:
  {aid}`, marks the job `completed`. `FingerprintResult` has no `target_id`/
  `target_version` field — only `aggregate_decision`, `confidence`,
  `dinov2_similarity`, `phash_score`, `audio_score`, `temporal_verified`,
  `algorithm_versions`, `worker_id`, `matched_title` (free text).
- **Recovery:** `reclaim_expired_jobs(batch_size)` — idempotent, safe to
  call redundantly/concurrently; not run automatically, a caller (the
  bridge, mirroring `CrawlerManager`'s own recovery loop) must schedule it.
- **Data available on a claimed job:** `asset_id`, `canonical_url`,
  `media_type`, `priority`, `retry_count`, `lease_expires_at`, plus (via one
  extra `HGETALL evidence:asset:{aid}`) `source_domain`, `mime_type`,
  `first_seen`/`last_seen`, discovery provenance. **Not available, at any
  layer of this repo: any notion of which reference movie/content a
  candidate should be checked against.**

## 4. Existing fingerprinter contract (destination side) — CURRENT IMPLEMENTATION

- **Entry point:** `integration.submission.FingerprintJobSubmitter.submit
  (candidate: FingerprintCandidate) -> SubmissionResult`.
- **`FingerprintCandidate` fields** (`integration/candidate.py`):
  `candidate_url`, `media_evidence_id`, `media_type`, `source_domain`,
  `target_id`, `target_version`, `techniques` (default `(DINOV2_TEMPORAL_
  TECHNIQUE,)`), `priority` (`FingerprintPriority.HIGH/NORMAL/LOW`, default
  `NORMAL`), `max_attempts` (default 3).
- **Validation (`candidate.validate()`, called first inside `submit()`):**
  `candidate_url` must start with `http://`/`https://`; **`media_evidence_
  id`, `media_type`, `source_domain`, `target_id`, `target_version` must
  all be non-empty strings** (`CandidateValidationError` otherwise);
  `techniques` must be non-empty; `max_attempts >= 1`.
- **Idempotency:** `job_id = sha256(candidate_url, target_id,
  target_version, sorted(techniques))[:32]` (`integration/idempotency.py`)
  — deterministic; a `SET NX EX <24h>` marker at `fingerprint:submission:
  {job_id}` (`integration/keys.py`) makes duplicate suppression a single
  atomic round trip, checked *after* backpressure and released if the
  subsequent `XADD` fails.
- **Backpressure:** `count_outstanding()` reads `XINFO GROUPS` for the
  target priority stream (`lag + pending`); rejects with
  `REJECTED_BACKPRESSURE` at `>= max_outstanding_jobs` (default 500,
  PROVISIONAL) or if Redis can't report a reliable `lag` (fails toward
  rejecting).
- **Destination stream:** `fingerprint:jobs:stream:{priority}` (`work_
  queue/keys.py::stream_key`; `{priority}` ∈ `{"high", "default", "low"}`).
  `XADD` via `work_queue.producer.JobProducer.enqueue()`, unchanged Phase 1
  code.
- **Consumer group:** `fingerprinter-workers` (`work_queue/keys.py::
  CONSUMER_GROUP`), consumed by `worker.fingerprint_worker.Worker`
  (`XREADGROUP`/`XAUTOCLAIM`, unchanged Phase 1-2 code), run via `python -m
  worker.main`.
- **Message schema (`work_queue.jobs.Job`, all fields required and
  validated on read):** `job_id`, `media_evidence_id`, `media_url`,
  `media_type`, `source_domain`, `target_id`, `target_version`,
  `techniques`, `max_attempts`, `schema_version`. **`target_id` and
  `target_version` are both hard-required, non-empty, string fields at
  every layer** (`FingerprintCandidate.validate()`,
  `Job.from_stream_fields()`).
- **`submit()` return values:** `ENQUEUED` (with `entry_id`),
  `DUPLICATE_SUPPRESSED`, `REJECTED_BACKPRESSURE`, `REJECTED_INVALID` — no
  exception for any of these four outcomes; a Redis connection failure is
  *not* caught and propagates as-is (infrastructure failure, not a
  candidate-level rejection — by design, `phase-12` doc §16).
- **Reading a result back:** `integration.outcome.resolve_outcome(job_id)`
  → `FingerprintOutcome` (`PENDING`/`MATCH`/`NO_MATCH`/`SKIPPED`/
  `RETRYABLE_ERROR`/`PERMANENT_ERROR`), folding `work_queue.state.
  JobStateStore` + `work_queue.results.ResultStore`.

## 5. Field mapping (as far as it can go)

| Crawler evidence job field | Fingerprinter `FingerprintCandidate` field | Status |
| --- | --- | --- |
| `asset_id` (sha256 of canonical URL) | `media_evidence_id` | Direct — crawler's own opaque back-reference, exactly what this field is for (fingerprinter never interprets it) |
| `canonical_url` | `candidate_url` | Direct |
| `media_type` | `media_type` | Direct (both use the same vocabulary: `video`, `audio`, `stream-manifest`, image types) |
| `evidence:asset:{aid}.source_domain` | `source_domain` | Direct (one extra `HGETALL`, already read by the store's own `list_media_assets`) |
| `priority` (int, lower = more urgent, ZSET score) | `priority` (`FingerprintPriority` enum, selects a Stream) | **Needs a mapping function, not a type match** — no calibrated threshold exists on either side; fingerprinter's own Phase 12 doc explicitly declined to invent one ("§12: deliberately not built... inventing a threshold mapping without calibration data would be exactly the kind of unjustified guess the brief warns against"). A bridge would need its own explicit, documented, configurable threshold (e.g. crawler priority ≤ N → HIGH) rather than inventing one silently. |
| `retry_count` | *(not part of `FingerprintCandidate`; bridge's own retry loop, not passed through)* | N/A — the fingerprinter has its own independent `max_attempts`/backoff; the crawler's `retry_count` describes *bridge-delivery* attempts, a different concept, and must not be conflated with the fingerprinter's own retry counter |
| *(none — bridge default)* | `techniques` | Bridge can safely default to `(DINOV2_TEMPORAL_TECHNIQUE,)`, the `FingerprintCandidate` default, since the crawler has no technique-selection concept at all |
| *(none — bridge default)* | `max_attempts` | Bridge can safely default to `FingerprintCandidate`'s own default (3) |
| **`target_id`** | **`target_id` (required, non-empty)** | **BLOCKED — see §7** |
| **`target_version`** | **`target_version` (required, non-empty)** | **BLOCKED — see §7** |

## 6. What the bridge could safely do, mechanically (contracts alone)

Independent of §7, the audit confirms the delivery-semantics design the
brief requires is achievable with these two existing contracts, with no
new schema and no modification to either side:

- **Recoverable claim:** the source ZSET's claim/lease/token model means a
  bridge process can `claim_next_fingerprint_job()`, and if it crashes
  before `complete_fingerprint_job()`/`fail_fingerprint_job()`, the
  crawler's own `reclaim_expired_jobs()` (run periodically by the bridge,
  same pattern as `CrawlerManager._recovery_loop`) makes the job claimable
  again after `fingerprint_lease_ttl` (900s default) — Case C in the brief
  is fully covered by *existing* crawler machinery, no new mechanism
  needed.
- **Destination submission failure (Case B):** `submit()` either returns a
  `SubmissionResult` (four non-exception outcomes) or raises (Redis
  unreachable). Either way, the bridge would call `fail_fingerprint_job(...,
  retryable=True)` rather than `complete_fingerprint_job(...)`, so the
  source job is never acknowledged and becomes retry-eligible under the
  crawler's own backoff — the job cannot silently disappear.
- **Duplicate delivery (Case D — bridge crashes after successful `submit()`
  but before source-side ack):** the destination's `job_id` is
  deterministic (`sha256(candidate_url, target_id, target_version,
  techniques)`) and the `fingerprint:submission:{job_id}` `SET NX` marker
  (24h TTL) makes a second `submit()` call for the *same* candidate return
  `DUPLICATE_SUPPRESSED`, not a second stream entry. **This gives the
  bridge idempotent re-delivery on the destination side** — a crash-and-
  retry bridge can safely re-submit the same candidate without creating a
  duplicate fingerprint job, *provided* it re-derives the same `target_id`/
  `target_version`/`candidate_url`/`techniques` both times (deterministic
  hash inputs). Combined with source-side at-least-once redelivery (the
  claim isn't acked until the bridge confirms `ENQUEUED` or
  `DUPLICATE_SUPPRESSED`), the overall system is **effectively-once from
  the fingerprinter's point of view, at-least-once from the source
  queue's point of view** — this would have been the phase's answer to the
  "Delivery Semantics" section, had §7 not blocked construction of the
  `job_id`'s own required inputs.
- **Malformed source job:** a job with an empty/invalid `canonical_url`
  (shouldn't occur — the store validates this at `record_media_link` time
  — but must be handled defensively) would fail
  `FingerprintCandidate.validate()` before any Redis write on the
  destination side; the bridge should route this to
  `fail_fingerprint_job(..., retryable=False)` (crawler's own
  `permanent_failure` state), never an infinite retry loop, mirroring how
  `Job.from_stream_fields()` itself treats a validation failure as
  `rejected`, not `retry_scheduled`.

None of this required inventing a new schema, a new queue structure, or
touching either system's retry/lease internals — exactly the "adapter, not
redesign" mandate. It is documented here, unimplemented, because §7 makes
it impossible to actually construct a submittable `FingerprintCandidate`
today.

## 7. BLOCKING FINDING — target/movie identity cannot be constructed

**STOP CONDITION (per this phase's brief, items 2 and 3): the crawler's
evidence job carries no information from which `target_id` or
`target_version` can be derived, and both are hard-required, validated,
non-empty fields on every message the fingerprinter's contract accepts.**

Evidence, both directions:

- **Crawler side:** exhaustive grep across the entire crawler repo
  (excluding `env/`) for `target_id`, `target_version`, `watchlist`,
  `catalog`, `movie_id`, `content_id`, `title_id` turns up no reference-
  content catalog anywhere. `seeds/*.txt` (`config.yaml`'s `seed_files`)
  list piracy *site* URLs to crawl, not protected titles to check against.
  `storage/media_evidence_store.py::FingerprintResult` (the crawler's own
  model of a fingerprinter answer) carries only a free-text
  `matched_title: Optional[str]`, populated only after a match is already
  confirmed — there is no *input*-side field anywhere upstream of that. A
  `content_id` column exists in the SQLite schema
  (`storage/sqlite_media_evidence_store.py`) and is defensively read at
  `storage/redis_media_evidence_store.py:605`, but is never written
  anywhere in the codebase — dead/vestigial, not a real identifier.
- **Fingerprinter side:** `target/registry.py::TargetRegistry.
  register_target(target_id, target_version, media_path, media_metadata)`
  is the only way a `(target_id, target_version)` pair becomes valid —
  it upserts an opaque, caller-supplied pair with no format validation and
  no lookup against any external catalog. `docs/design/design-proposal-
  1.md` (the fingerprinter's founding document, line 229) states this
  explicitly: *"Target identity: `target_id` — an identifier assigned by
  rights-holder/ops tooling, independent of any crawler asset id."* The
  same document's own "deferred / explicitly out of scope" list (line 273)
  names *"Target ingestion/registration workflow and API surface (who
  creates `target_id`s and uploads content)"* as an **unresolved, open
  question in the fingerprinter's own design** — not an oversight this
  audit can safely paper over.
- **Live state:** `KEYS fingerprint:target:*` against the shared
  production Redis instance (`localhost:6379`, DB 0) returns **zero
  results** — no target has ever actually been registered in this
  environment. There is no "the one target this deployment protects"
  convention to fall back on either; nothing in either repo's
  documentation or code asserts or assumes a single implicit target.
- **Worker-side consequence if this were faked anyway:** `worker/matching_
  handler.py`'s target resolution raises `KeyError` for an unknown
  `(target_id, target_version)`, which Phase 12's own failure table maps to
  `PermanentFailure` → `PERMANENT_ERROR` — a bridge that invented a
  placeholder `target_id` (e.g. `"unknown"`) would not fail loudly at
  submission time (validation only checks non-empty, not existence); every
  job would silently reach a worker, resolve to `KeyError`, and terminate
  as a permanent failure with a confusing error class, burning a
  fingerprint worker's DINOv2 inference cost (the fingerprinter's own
  Phase 11 finding: ~95% of warm-cache job latency) on jobs that were
  guaranteed to fail before they were ever created. This is strictly worse
  than not submitting the job at all.

**Per the brief's explicit instruction ("If target/movie identity cannot
safely be determined, STOP and report it" / "DO NOT fabricate a target
ID"), no `target_id`/`target_version` value is invented here, and the
bridge is not implemented.**

## 8. Smallest possible unblocking step

This is a product/ops decision, not a code change this phase can make
unilaterally:

1. **Decide how targets (the movies/content being protected) actually get
   registered** — a rights-holder-facing tool, an ops CLI, a manual
   one-time seeding script, or an API — and who owns building it. This is
   explicitly out of scope for both this crawler repo and the
   fingerprinter repo's Phase 1-12 (`design-proposal-1.md` line 273); it
   requires a decision this audit has no authority to make.
2. Once at least one `(target_id, target_version)` pair is registered
   (`TargetRegistry.register_target(...)`, fingerprinter side) **and**
   the crawler (or an operator) has a way to know *which* registered
   target a given discovered candidate should be checked against (even a
   trivial "the crawl is currently scoped to protecting exactly one title,
   configured once" convention would be enough to unblock — but that
   convention does not exist today and this phase will not invent it),
   the bridge described in §3-§6 above can be implemented essentially as
   specified: the claim/lease/retry mechanics, the idempotent
   effectively-once delivery argument, and the field mapping are all
   already fully worked out and require no further design once `target_id`/
   `target_version` have a real source.
3. **This phase's recommendation:** do not attempt a "smallest schema
   change" workaround (e.g. adding a `target_id` field to
   `evidence:asset:{aid}` that nothing ever populates) — that would create
   the same false sense of completeness the brief's STOP conditions exist
   to prevent. The gap is a missing *process* (target registration), not a
   missing *field*.

## 9. What was NOT done, and why

Per the brief's explicit STOP-condition instructions:

- No bridge code was written. §6 documents the design that would be used
  once §7 is resolved, so the eventual implementation is not starting
  from zero design work, but no `.py` file implementing it exists yet.
- No changes were made to either repository beyond this document.
- The fingerprinter repository was not modified (read-only per the
  brief's explicit constraint) and confirmed unmodified: `git status`/
  `git diff` were not run there since no edits were made.
- Pre-existing uncommitted changes in this repo (`crawler/*.py`,
  `utils/url_utils.py`, `seeds/piracy_sites.txt`, three test files —
  Phase 1's blacklist fix, per `git log`/`git status` at the start of this
  phase) were left untouched; this phase's only filesystem change is this
  document.

## 10. Deferred work

Everything in the brief's "PREFERRED ARCHITECTURE" / "TEST REQUIREMENTS" /
"CONTROLLED END-TO-END SMOKE TEST" sections is deferred until §7/§8 are
resolved:

1. Target registration workflow/ownership decision (§8) — blocking
   everything below.
2. Bridge implementation (adapter process/module, §6's design).
3. Deterministic tests (forwarding, field mapping, ack-after-success,
   destination-failure recovery, crash/reclaim, duplicate suppression,
   malformed-job handling, empty-queue idle behavior, multi-job ordering).
4. Real-Redis integration test, isolated namespace.
5. Controlled end-to-end smoke test proving a fingerprinter worker claims
   a bridge-forwarded job.
6. Bridge configuration surface, observability, and the final version of
   this document's delivery-semantics/failure-semantics/lease-recovery
   sections, written against actual code instead of the mechanical
   analysis in §6.
