# Phase 4 — Crawler → Fingerprinter Bridge (Implementation)

## Status: COMPLETE

## 1. Objective

Build the actual bridge connecting the crawler's existing, working
fingerprint-job producer (`evidence:jobs:queue`, a Redis ZSET, this repo)
to the fingerprinter's existing, working fingerprint-job consumer
(`fingerprint:jobs:stream:{priority}`, a Redis Stream + consumer group,
the sibling `fingerprinter` repo), without redesigning or modifying either
system's core contract, without merging the two repositories'
environments, and without fabricating target identity. Phase 3
(`docs/architecture/phase-3-target-registration-and-scoping.md`) removed
the blocker the prior Phase 3 audit
(`docs/architecture/phase-3-crawler-fingerprinter-bridge.md`) found: every
target-scoped fingerprint job now carries a real, operator-registered
`target_id`/`target_version`. This phase is the bridge itself.

## 2. Existing contracts inspected (read-only, both repos)

**Crawler (this repo):** `storage/media_evidence_store.py`,
`storage/redis_media_evidence_store.py`,
`storage/sqlite_media_evidence_store.py`, `core/config.py`,
`core/crawler_manager.py`, `core/target_scope.py`, `main.py`,
`tests/fingerprinter_queue_test.py`,
`tests/redis_media_evidence_store_test.py`, `tests/target_scope_test.py`,
plus the two prior Phase 3 architecture documents in full.

**Fingerprinter (sibling repo, read-only):**
`integration/candidate.py`, `integration/idempotency.py`,
`integration/keys.py`, `integration/submission.py`,
`integration/backpressure.py`, `work_queue/jobs.py`,
`work_queue/producer.py`, `work_queue/keys.py`, `target/registry.py`,
`target/keys.py`, `worker/main.py`, `worker/fingerprint_worker.py`,
`matching/aggregation.py` (for `DINOV2_TEMPORAL_TECHNIQUE`'s value),
`docs/architecture/phase-12-crawler-fingerprinter-integration.md`,
`docs/design/design-proposal-1.md`. No file in the fingerprinter repo was
modified — confirmed by `git status` there before and after this phase
(untracked `benchmark/`, `benchmarks/results/overnight_e2e_fingerprinter.json`,
`scripts/` predate this phase and were left untouched).

## 3. Architecture decision

**The bridge lives entirely in this repo** (`bridge/` package), running
under this repo's own `env/` — not the fingerprinter's `.venv/`, not a
third deployment unit. Two reasons, both load-bearing:

1. **Rule 8 ("reuse existing claim/lease/reclaim semantics") requires an
   in-process Python object**: `RedisMediaEvidenceStore.claim_next_
   fingerprint_job()`/`fail_fingerprint_job()`/`reclaim_expired_jobs()`
   *are* the claim/lease/retry contract — there is no wire-protocol
   equivalent to call instead. That class is defined in this repo, so the
   bridge must run inside this repo's own environment to use it directly
   (an in-repo import, not a cross-repo one).

2. **The fingerprinter's `integration.submission.FingerprintJobSubmitter`
   cannot be imported from here** without either merging environments or a
   direct cross-repo import — both explicitly forbidden. Per this phase's
   brief: *"If the current FingerprintJobSubmitter only exists as an
   in-process Python API and cannot be used from the crawler repository
   without violating the independent-repository rule, design a minimal
   Redis-native bridge adapter inside the appropriate repository rather
   than introducing cross-repo imports."* `bridge/fingerprint_stream_
   adapter.py` is exactly that: a byte-for-byte replica of
   `FingerprintJobSubmitter.submit()`'s Redis operations (job-id
   derivation, backpressure read, submission-marker CAS, `XADD` field
   schema), built with nothing but `redis-py`, verified against the real
   fingerprinter source (§10) — not guessed.

Both sides talk to the **same physical Redis instance** (confirmed by the
prior Phase 3 bridge audit, §7, and unchanged since) via the evidence
store's own `redis_conn` — the bridge never opens a second connection.

**One additive contract extension was required and is documented
prominently (§6).**

## 4. Source queue semantics (unchanged, reused as-is)

`evidence:jobs:queue` (ZSET, namespace `evidence` in production) —
`RedisMediaEvidenceStore.claim_next_fingerprint_job(worker_id)` atomically
pops the head, returns a `FingerprintJob(asset_id, token, canonical_url,
media_type, priority, retry_count, lease_expires_at, target_id,
target_version)`. `token` is the CAS ownership proof for every subsequent
call. Lease TTL, retry/backoff, and `reclaim_expired_jobs()` are all
pre-existing, Phase-1/Phase-3 machinery — the bridge calls these APIs, it
does not reimplement them.

## 5. Destination queue semantics (unchanged, replicated exactly)

`fingerprint:jobs:stream:{priority}` (`{priority}` ∈ `high`/`default`/`low`),
consumer group `fingerprinter-workers`. A submission is: validate
(`FingerprintCandidate`-equivalent checks) → `count_outstanding()` read
(backpressure) → `SET NX EX` on `fingerprint:submission:{job_id}` (dedup) →
`XADD` with `Job.to_stream_fields()`'s exact schema → release the marker if
`XADD` itself fails. `job_id = sha256(candidate_url, target_id,
target_version, sorted(techniques))[:32]`. All replicated in
`bridge/fingerprint_stream_adapter.py`, verified identical to the real
contract (§10).

## 6. Mapping — and the one contract extension this phase made

| Crawler `FingerprintJob` field | Fingerprinter wire field | Source |
| --- | --- | --- |
| `canonical_url` | `candidate_url` / `media_url` | direct |
| `asset_id` | `media_evidence_id` | direct |
| `media_type` | `media_type` | direct |
| *(one extra `HGET` on `evidence:asset:{aid}.source_domain`)* | `source_domain` | Phase 3 bridge audit §5's own prescribed mapping |
| `target_id`, `target_version` | `target_id`, `target_version` | direct (Phase 3) |
| `priority` (int, lower=more urgent) | `priority` band (`high`/`default`/`low`) | explicit configurable threshold, §8 |
| *(bridge default)* | `techniques = ("dinov2",)` | fingerprinter's own single-technique default (`DINOV2_TEMPORAL_TECHNIQUE`, verified §10) |
| *(bridge default)* | `max_attempts = 3` | fingerprinter's own `FingerprintCandidate` default |

**Contract extension: `JOB_FORWARDED` (`storage/media_evidence_store.py`).**
The existing evidence-job state machine has exactly three outcomes reachable
from `claimed`: `complete_fingerprint_job(result)` (requires a real
`FingerprintResult` verdict — `confirmed`/`rejected`/`uncertain`),
`fail_fingerprint_job(retryable)`, or lease expiry. **None of these mean
"successfully handed off to the fingerprinter, verdict not yet known"** —
and the bridge has no verdict to report at hand-off time. Calling
`complete_fingerprint_job()` with a fabricated decision (e.g. `"uncertain"`)
would make a forwarded-but-unprocessed asset indistinguishable from an
actually-checked one to any reader of `list_media_assets()` — exactly the
fabrication rule 12 forbids, and strictly worse than not forwarding at all.

This phase therefore added one new terminal status, `JOB_FORWARDED =
"forwarded"`, and one new store method,
`mark_fingerprint_job_forwarded(asset_id, token, *, fingerprint_job_id)`,
implemented identically in both backends (`RedisMediaEvidenceStore` via a
new CAS'd Lua script mirroring `_complete_script`/`_fail_script`'s shape;
`SQLiteMediaEvidenceStore` via the same CAS pattern plus one new nullable
`fingerprint_job_id` column). This is **additive only** — no existing
method signature, Lua script, or status changed; every pre-existing call
site is unaffected (verified: full pre-existing suite still passes, §12).
It follows the exact precedent Phase 3 set when it added `target_id`/
`target_version` as new optional `FingerprintJob` fields. The actual
fingerprinting verdict, once a future consumer exists to read the
fingerprinter's result contract, is still recorded through the unchanged
`complete_fingerprint_job()` path — out of this phase's scope (§16).

## 7. Target propagation

Every forwarded job carries the exact `target_id`/`target_version` already
fixed on the evidence job at creation time (Phase 3) — never inferred,
never fabricated. Two independent guards, both before any `XADD`:

1. **Missing scope**: if `job.target_id`/`job.target_version` is `None`
   (an unscoped crawler run), the bridge permanently rejects the job
   (`error_class=missing_target_scope`) rather than fabricating a value.
2. **Defense-in-depth re-check**: even though Phase 3 already validated the
   target at crawler-run startup, the bridge re-runs
   `core.target_scope.verify_target_registered()` (an in-repo reuse, one
   cheap `EXISTS`) immediately before forwarding, in case the registration
   window has changed since. An unregistered target is permanently
   rejected (`error_class=target_not_registered`) instead of burning a
   fingerprint worker's DINOv2 cost on a job guaranteed to fail.

**TESTED**: `tests/bridge_test.py::TestTargetPropagation`,
`TestValidForwarding::test_valid_job_is_forwarded_with_all_fields_intact`
(asserts the exact `target_id`/`target_version` string values in the real
XADD'd stream entry).

## 8. Priority propagation

Crawler priority is a plain `int`, lower = more urgent (default `10`,
unbounded range). The fingerprinter has three named streams
(`high`/`default`/`low`) and, per its own Phase 12 doc §12, deliberately
never built a threshold mapping — leaving that decision to "a future
bridge component." This phase is that component:
`core.config.BridgeConfig` adds two new, documented, operator-tunable
fields:

```yaml
crawler:
  bridge:
    priority_high_max: 5    # crawler priority <= this -> fingerprinter HIGH
    priority_low_min: 15    # crawler priority >= this -> fingerprinter LOW
                             # everything between (including the default, 10) -> NORMAL
```

**PROVISIONAL** (no calibration data exists for these specific numbers, same
caveat the fingerprinter's own `DEFAULT_MAX_OUTSTANDING_JOBS` carries) but
never a silent flatten-to-one-priority. **TESTED**:
`tests/bridge_test.py::TestPriorityPropagation` (parametrized over the
crawler's default `10` and boundary values, asserting the job lands on the
correct stream and no other).

## 9. Duplicate semantics

**At-least-once from the source queue's point of view, effectively-once
from the fingerprinter's point of view** — identical to what the prior
Phase 3 bridge audit's §6 worked out mechanically, now implemented and
tested against real Redis:

- `job_id` is deterministic (`sha256(candidate_url, target_id,
  target_version, sorted(techniques))`) — a bridge crash-and-retry
  re-derives the *same* `job_id` for the same candidate.
- The fingerprinter's own `SET NX EX` submission marker
  (`fingerprint:submission:{job_id}`, 24h default TTL, unchanged
  fingerprinter contract) makes a second `submit_job()` call for the same
  candidate return `DUPLICATE_SUPPRESSED`, never a second stream entry.
- The bridge treats `DUPLICATE_SUPPRESSED` identically to `ENQUEUED` for
  the purpose of acknowledging the source job (§11) — "already forwarded"
  and "just forwarded" both mean the source job is done.

**TESTED**:
`tests/bridge_test.py::TestCrashAndDuplicateRecovery::test_crash_after_xadd_before_ack_is_recovered_without_duplicating_the_stream_entry`
— a real `submit_job()` call XADDs a real entry, the claim is force-expired
(simulating a bridge crash between XADD and ack) and reclaimed, and a fresh
bridge instance's retry is proven to hit `DUPLICATE_SUPPRESSED` while
`XLEN` on the real stream stays at exactly `1`.

## 10. Wire-contract fidelity (verified, not assumed)

`bridge/fingerprint_stream_adapter.py`'s docstring names the exact
fingerprinter source files every constant/algorithm was copied from. This
is independently **VERIFIED** by
`tests/bridge_stream_adapter_test.py::TestDeriveJobId::test_job_id_and_stream_fields_match_the_real_fingerprinter_contract`,
which shells out to the *actual* `fingerprinter/.venv/bin/python3` (never a
Python import — a subprocess, so no cross-repo import is introduced) to
compute `derive_job_id()`/`Job.to_stream_fields()` from the real
fingerprinter source for a fixed input, and asserts this repo's replica
produces byte-identical output. Confirmed passing (§12). If the
fingerprinter repo's contract ever changes, this is the test that would
catch drift — there is no automatic cross-repo drift detection beyond it
(see §16, Limitations).

## 11. Delivery semantics / crash safety (rule 6's failure windows)

| Case | What happens | Source state | Duplication? | Loss? |
| --- | --- | --- | --- | --- |
| A. crash before claim | nothing happened yet | untouched (`queued`) | no | no |
| B. crash immediately after claim | claim recorded, lease running | `claimed`, lease will expire → reclaimed → retried | no | no |
| C. crash during target/candidate validation | no Redis write on the destination side yet | `claimed` → lease expiry → reclaimed | no | no |
| D. crash before `XADD` | same as C | `claimed` → reclaimed | no | no |
| E. crash after `XADD`, before source ack | destination durable, source still `claimed` | lease expiry → reclaimed → re-processed → `DUPLICATE_SUPPRESSED` → correctly marked `forwarded` on retry | **no** (destination dedup, §9) | no |
| F. crash after source ack (`forwarded`) | fully done | `forwarded` (terminal) | no | no |
| G. Redis unavailable while forwarding | `redis.RedisError`/`MediaEvidenceUnavailable` propagates uncaught out of `process_one()`; `run_forever()` catches it one level up, logs, backs off, retries next iteration | `claimed`, lease will expire → reclaimed | no | no |
| H. fingerprinter stream unavailable | same physical Redis as G in this deployment (Phase 3 audit §7) — reduces to G in practice | as G | as G | as G |
| I. malformed evidence job (e.g. empty `source_domain`) | `validate_candidate_fields()` raises before any destination write | `fail_fingerprint_job(retryable=False)` → `permanent_failure` immediately (no infinite retry) | no | intentional — a job that can never be valid is not silently dropped, it is visibly, permanently rejected |
| J. target registration disappears | defense-in-depth `verify_target_registered()` re-check (§7) catches this before forwarding | `permanent_failure` (`target_not_registered`) | no | intentional, same reasoning as I |
| K. bridge restarted after a crash | a fresh `CrawlerFingerprinterBridge` instance, same claim/lease/reclaim APIs — no special restart logic needed, the source queue's existing recovery already covers this | as B/E depending on when the crash happened | as B/E | no |

**TESTED** (real Redis, not mocks): `TestAckOrdering` (case G — a
monkeypatched `xadd` failure proves the source job is left exactly
`claimed`, not falsely `forwarded` or `failed`, and is still recoverable
after a forced lease expiry), `TestCrashAndDuplicateRecovery` (case E),
`TestRestartRecovery` (case K), `TestInfrastructureFailure` (case G at the
claim layer, plus `run_forever()`'s backoff-and-continue behavior).

**Rule 5's requirement — never falsely claim exactly-once**: this document
states at-least-once-from-the-source, effectively-once-from-the-destination,
explicitly, and no test or code path asserts stronger. A pathological
sequence (bridge crash exactly between the destination `SET NX` succeeding
and the `XADD` itself — a window inherited unchanged from the fingerprinter's
own `FingerprintJobSubmitter.submit()`, not introduced by this phase) could
in principle leave a claimed submission marker with no stream entry; the
fingerprinter's own `submit()` closes this by deleting the marker on `XADD`
failure (§5, "release the marker if XADD itself fails") — this bridge
replicates that exact behavior (`fingerprint_stream_adapter.py::submit_job`).

## 12. Retry semantics

Rule 8: **no second retry implementation was built.** Every retry path
reuses `RedisMediaEvidenceStore.fail_fingerprint_job()`'s existing
exponential-backoff/`max_retries` machinery:

- **Malformed candidate / missing or unregistered target** → `retryable=False`
  → immediate `permanent_failure` (never worth retrying, per rule "never
  infinite retry loop").
- **Destination backpressure** (`REJECTED_BACKPRESSURE`) → `retryable=True`
  → the job re-enters the crawler's own backoff/retry budget
  (`max_retries`, default 2), exactly like any other retryable failure.
  **Known tradeoff, documented, not fixed**: this reuses the same counter
  a hypothetical future fingerprinting-attempt-retry consumer would also
  want to use — acceptable today because no such consumer exists yet
  (Phase 12 doc §2/§15), and reuse was explicitly preferred over a second
  retry mechanism.
- **Successful hand-off** (`ENQUEUED` or `DUPLICATE_SUPPRESSED`) →
  `mark_fingerprint_job_forwarded()` (§6) — terminal, not a retry outcome.
- **Bridge/infrastructure crash before any of the above** → the crawler's
  existing `reclaim_expired_jobs()` (unmodified, reused verbatim by the
  bridge's own periodic sweep, mirroring `CrawlerManager._recovery_loop`'s
  use of the frontier's equivalent) requeues the job for another claim
  attempt once the lease expires.

**TESTED**: `TestBackpressure` (a rejected job becomes `retry_scheduled`,
not lost, and forwards successfully on a later retry once the synthetic
backlog clears).

## 13. Failure classification (rule 14)

Kept as four separate classes, never conflated, never routed through
`core.network_health.HealthController` (that system is this process's own
Internet-reachability detector — an unrelated failure domain the bridge
does not touch):

1. **Target/network failure** (the crawler's own Internet connectivity) —
   entirely out of the bridge's scope; the bridge has no code path that
   reads or writes `HealthController` state.
2. **Redis infrastructure failure** (source or destination — same physical
   instance in this deployment) — `redis.RedisError`/
   `MediaEvidenceUnavailable`, caught only at `run_forever()`'s outer loop,
   logged, backed off, never converted into a job-level failure.
3. **Malformed candidate** — `CandidateValidationError` → permanent,
   non-retryable job failure.
4. **Fingerprinter rejection** — currently only `REJECTED_BACKPRESSURE`
   is reachable from this bridge (`REJECTED_INVALID` can't occur here
   since the bridge validates before calling `submit_job()`) → retryable
   job failure.

## 14. Observability

Structured `loguru` log lines (this repo's existing convention) at every
stage rule 15 lists: `bridge: claimed ...`, `bridge: submitted ...` /
`bridge: duplicate suppressed ...`, `bridge: permanent reject ...`,
`bridge: retryable failure ...`, `bridge: Redis error ...`, plus a final
`bridge: shutdown metrics=...` line. Every log line carries `asset_id`
and, once known, `job_id`/`target_id`/`target_version`/`priority` — never
full media metadata blobs. `BridgeMetrics` (a plain dataclass, matching
this repo's existing counter-style rather than introducing a metrics
framework) tracks every counter rule 16 lists:
`jobs_claimed`/`jobs_submitted`/`jobs_acknowledged`/`jobs_retried`/
`jobs_rejected`/`jobs_failed`/`jobs_duplicated`/`active_jobs`. Redis-side,
`get_status_counts()` now also reports a `forwarded` count (mirroring the
existing `completed_total` counter pattern) for ops introspection.

## 15. Configuration

New, additive-only `core.config.BridgeConfig` (defaults shown; `config.yaml`
itself was **not modified** — every field has a default, matching this
repo's existing convention for optional sections):

```yaml
crawler:
  bridge:
    max_outstanding_jobs: 500       # mirrors fingerprinter's own default
    submission_marker_ttl_s: 86400  # mirrors fingerprinter's own default
    priority_high_max: 5
    priority_low_min: 15
    max_attempts: 3
    poll_idle_sleep_seconds: 2.0
    reclaim_interval_seconds: 60.0
```

Run as its own process: `crawler/env/bin/python3 -m bridge.main
[--config config.yaml] [--worker-name bridge-1] [--once]`. Requires the
Redis media-evidence backend (fails clearly at startup otherwise — target
scoping only exists there, so an SQLite-backed run has no legally
forwardable job in the first place).

## 16. Tests

**New files** (crawler repo): `tests/bridge_stream_adapter_test.py` (22
tests — wire-contract fidelity including the real-fingerprinter
cross-check, candidate validation, submission/backpressure/duplicate/
priority routing against real Redis), `tests/bridge_test.py` (19 tests —
full orchestration: valid forwarding, target/priority/field propagation,
missing/unregistered-target rejection, malformed-candidate rejection,
backpressure retry, ack-only-after-destination-durability, crash+duplicate
recovery, Redis-unavailable handling, graceful shutdown, restart
recovery). **Extended**: `tests/redis_media_evidence_store_test.py` (+4,
`TestMarkForwarded`), `tests/fingerprinter_queue_test.py` (+1, SQLite
parity for `mark_fingerprint_job_forwarded`). All against real local Redis,
test DB 1 (this repo's existing convention), never DB 0. All skip cleanly
if Redis is unavailable, matching every other Redis-backed test in this
repo.

Maps directly onto the brief's STEP 4 checklist (1–15): items 1–5 → the
adapter tests + `TestValidForwarding`/`TestPriorityPropagation`; 6 →
`TestMalformedCandidate`; 7 → `TestTargetPropagation::test_missing_target_scope...`;
8 → `TestTargetPropagation::test_unregistered_target...`; 9 →
`TestBackpressure`; 10 → `TestAckOrdering`; 11 → `TestRestartRecovery`; 12
→ `TestCrashAndDuplicateRecovery`; 13 → `TestInfrastructureFailure`; 14 →
`TestGracefulShutdown`; 15 → `TestRestartRecovery`.

## 17. Test commands and results — TESTED

```text
$ env/bin/python3 -m pytest -q tests/bridge_test.py tests/bridge_stream_adapter_test.py -m "not slow"
40 passed, 1 deselected in 0.26s

$ env/bin/python3 -m pytest -q tests/bridge_stream_adapter_test.py -m "slow"
1 passed in 3.29s   # real-fingerprinter cross-check subprocess (~30s cold import, cached warm)

$ env/bin/python3 -m pytest -q tests/fingerprinter_queue_test.py tests/redis_media_evidence_store_test.py tests/media_evidence_test.py
37 passed in 2.95s

$ env/bin/python3 -m pytest -q tests/
397 passed, 2 skipped, 1 deselected, 1 failed in 77.54s
FAILED tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::
    test_concurrent_redis_calls_use_a_bounded_shared_thread_pool
```

That one failure is the **same pre-existing, timing-sensitive flake**
already documented in `docs/architecture/phase-2-crawler-fingerprint-job-trace.md`
§11 and `docs/architecture/phase-3-target-registration-and-scoping.md` §13
— the URL frontier's thread-pool-offload test, nothing this phase touched
(no frontier code, no `frontier_executor.py`, no `redis_frontier.py`).
Reproduced standalone immediately after the full-suite run:

```text
$ env/bin/python3 -m pytest -q tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool
1 passed in 0.16s
```

**0 regressions in every surface this phase touched or added.**

## 18. Real Redis validation — VERIFIED

Isolated test DB 1 throughout, production DB 0 never touched (confirmed
before and after by direct inspection). Steps actually run:

1. Real fingerprinter `TargetRegistry`, via its own
   `scripts/register_target.py` (fingerprinter's `.venv`), registered
   `target_id=phase4_smoke`, `target_version=v1` against
   `tests/fixtures/tiny_video.mp4` (fingerprinter's own existing test
   fixture) at `redis://localhost:6379/1`.
2. Real `core.crawler_manager.build_media_evidence_store()` (crawler's
   `env/`), config-scoped to that exact target, created one real evidence
   job for `http://127.0.0.1:8991/tiny_video.mp4` (a local HTTP server
   serving the same fixture) via `record_media_link()`. Startup log
   confirmed: `"Crawler run scoped to fingerprinter target 'phase4_smoke'
   version 'v1'"` — the real Phase 3 registry check passed against the
   real registration from step 1.
3. A real `CrawlerFingerprinterBridge.process_one()` call claimed and
   forwarded it. `redis-cli -n 1 xrange fingerprint:jobs:stream:high`
   showed exactly one entry with `target_id=phase4_smoke`,
   `target_version=v1`, `media_url=http://127.0.0.1:8991/tiny_video.mp4`,
   `media_type=video`, `techniques=dinov2`, `schema_version=1` — all
   fields correct. The evidence job's hash showed `status=forwarded`,
   `fingerprint_job_id` set to the real derived job id — **acknowledged
   only after** the real `XADD` succeeded.
4. All test keys (`fingerprint:target:phase4_smoke:v1`, its content-hash
   index entry, `fingerprint:jobs:stream:*`, `fingerprint:submission:*`,
   the `phase4_smoke_evidence:*` namespace) deleted afterward. Confirmed
   empty via `redis-cli -n 1 keys "*phase4_smoke*"` /
   `redis-cli -n 1 keys "fingerprint:*"` (both empty) and
   `redis-cli -n 0 keys "*phase4_smoke*"` (empty throughout — DB 0 was
   never touched by this phase).

## 19. Real fingerprinter-worker smoke test — VERIFIED (delivery + claim)

A second real evidence job (crawler priority `10` → the bridge's `NORMAL`
band → `fingerprint:jobs:stream:default`, the only stream
`worker/main.py`'s production entrypoint listens on — it has no
priority-selection configuration surface) was forwarded the same way, then
the **actual, unmodified** `worker.fingerprint_worker.Worker` was started
as a genuinely separate OS process, using the fingerprinter's own `.venv/`
(never installed into or mixed with this repo's `env/`):

```text
$ REDIS_URL=redis://localhost:6379/1 TARGET_CACHE_PATH=<scratch> \
  WORKER_CONSUMER_NAME=phase4-smoke-worker-2 EMBEDDING_DEVICE=cpu \
  TORCH_NUM_THREADS=1 fingerprinter/.venv/bin/python3 -m worker.main
```

Real worker log output (structured JSON, unmodified `worker/main.py`
format):

```json
{"event": "worker_ready", ..., "stream": "fingerprint:jobs:stream:default"}
{"event": "job_claimed", "job_id": "f8483188d6c17b68cc893d08aeeca1f5", "attempt": 1, ...}
{"event": "job_failed", "job_id": "f8483188d6c17b68cc893d08aeeca1f5", "attempt": 1,
 "error_type": "UnsafeDestinationError", "error_category": "permanent_acquisition_failure", ...}
```

The `job_id` in `job_claimed` is byte-identical to the `job_id` the bridge
computed and `XADD`'d — **this is the proof this phase's success condition
asks for: a bridge-forwarded job was claimed by a real, separate
fingerprint-worker process.** `redis-cli -n 1 xpending fingerprint:jobs:stream:default
fingerprinter-workers` returned `0` afterward — the worker committed a
clean terminal state (`fingerprint:job:{job_id}:state` shows
`status=failed`), not a dangling claim.

The subsequent `UnsafeDestinationError` is the fingerprinter's own,
**unmodified**, unrelated SSRF guard (`acquisition/ssrf_guard.py`) correctly
refusing to fetch a loopback URL (`127.0.0.1`, from the local test HTTP
server) — production `worker/main.py` runs with
`allow_private_networks=False` by design and has no configuration surface
to override it (only the fingerprinter's own e2e test suite, via a
different, test-only code path, sets that flag). Per the brief: *"This
test only needs to prove successful job delivery and worker claim. It does
NOT need to prove DINOv2 matching correctness."* Matching correctness was
not attempted and is not claimed. Both worker processes and the local HTTP
server were terminated (`SIGTERM`) and all test keys deleted identically
to §18 afterward.

## 20. Limitations

- **No automatic cross-repo drift detection beyond one test.** If the
  fingerprinter repo's `derive_job_id`/`Job.to_stream_fields`/key
  conventions ever change, `bridge/fingerprint_stream_adapter.py` must be
  updated by hand — `test_job_id_and_stream_fields_match_the_real_
  fingerprinter_contract` (§10) would fail and catch it, but only when
  actually run against a fingerprinter checkout with the new contract.
- **Priority threshold defaults are PROVISIONAL**, same status as the
  fingerprinter's own `DEFAULT_MAX_OUTSTANDING_JOBS` — no load-tested or
  calibrated data exists for either project yet.
- **Backpressure retries consume the same `max_retries` budget** a future
  fingerprinting-attempt-retry consumer might also want (§12) — accepted,
  documented, not fixed (no such consumer exists yet).
- **One bridge process, single-threaded, bounded work per iteration** — the
  brief's preferred default. Running several instances concurrently is
  safe (both the source claim and destination dedup are already
  multi-claimer-safe) but was not load-tested; nothing currently
  demonstrates a need for it.
- **The bridge does not consume fingerprinter results.** `resolve_outcome`/
  `complete_fingerprint_job()` wiring (turning a fingerprinter `MATCH`/
  `NO_MATCH` back into crawler evidence state) remains unbuilt — explicitly
  out of scope per the fingerprinter's own Phase 12 doc §15/§23 and this
  phase's own JOB_FORWARDED design (§6).
- **`worker/main.py` has no priority-stream selection.** Only the
  `default` stream is consumed by the production worker entrypoint today;
  `high`/`low` jobs the bridge forwards will queue until an operator runs
  a worker process configured for that stream (not a bridge limitation —
  restated here because it shaped the smoke test in §19).
- **Real network acquisition/SSRF behavior against genuinely external
  media URLs was not exercised** (the smoke test deliberately used a
  loopback URL, correctly rejected by the fingerprinter's own unmodified
  security layer) — out of scope; this phase proves delivery and claim,
  not acquisition success.

## 21. Deferred work

1. A crawler-side consumer of fingerprinter results (`resolve_outcome()` →
   `complete_fingerprint_job()`) — Phase 12 doc §15/§23's own explicitly
   deferred item, unchanged by this phase.
2. Load-testing/calibrating `priority_high_max`/`priority_low_min`/
   `max_outstanding_jobs` against real multi-host throughput.
3. A worker-process priority-selection configuration surface (fingerprinter
   repo, out of this phase's scope to add).
4. Running multiple concurrent bridge instances, if throughput ever
   demands it (mechanically safe today, not exercised under real load).

## 22. Exact files changed

**Crawler repo only** (fingerprinter repo: zero changes, confirmed by
`git status`):

New:
- `bridge/__init__.py`, `bridge/fingerprint_stream_adapter.py`,
  `bridge/crawler_fingerprinter_bridge.py`, `bridge/main.py`
- `tests/bridge_stream_adapter_test.py`, `tests/bridge_test.py`
- `docs/architecture/phase-4-crawler-fingerprinter-bridge.md` (this file)

Modified (additive only — see `git diff` for exact hunks):
- `storage/media_evidence_store.py` (+`JOB_FORWARDED`, protocol method)
- `storage/redis_media_evidence_store.py` (+Lua script, +method,
  +`forwarded_total` counter)
- `storage/sqlite_media_evidence_store.py` (+column, +method, parity)
- `core/config.py` (+`BridgeConfig`, attached as `CrawlerConfig.bridge`)
- `tests/redis_media_evidence_store_test.py` (+`TestMarkForwarded`, 4 tests)
- `tests/fingerprinter_queue_test.py` (+1 SQLite parity test)

`config.yaml` was **not modified** — every new field has a default.

Pre-existing uncommitted changes in this repo from before this phase
(`core/crawler_manager.py`, `crawler/*.py` engines, `main.py`,
`utils/url_utils.py`, `seeds/piracy_sites.txt`, three test files, plus
`storage/redis_media_evidence_store.py`/`sqlite_media_evidence_store.py`'s
own Phase 3 target-scope changes) were left exactly as found — this
phase's diff is additive on top of them, nothing was reverted or altered.

## 23. Final phase status

**Phase 4 is complete.** A real crawler evidence job travels through
`evidence:jobs:queue` → the bridge → `fingerprint:jobs:stream:{priority}`
→ a real, unmodified fingerprinter worker process, with target identity,
priority, and media fields intact, at-least-once delivery with
deterministic duplicate suppression, and the source job acknowledged only
after destination durability — proven against real local Redis and a real,
separate fingerprinter worker process, not mocks alone. No Phase 5 work
was started.
