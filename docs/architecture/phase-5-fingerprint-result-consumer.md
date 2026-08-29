# Phase 5 — Fingerprint Result Consumer (Implementation)

## Status: COMPLETE

## 1. Objective

Close the one gap Phase 4 explicitly deferred (`docs/architecture/
phase-4-crawler-fingerprinter-bridge.md` §20/§21, item 1): a fingerprint
`MATCH`/`NO_MATCH`/`processing_failure` verdict, once the fingerprinter
commits it, never reached this repo's own Media Evidence store. A job
forwarded by the Phase 4 bridge sat at `status=forwarded` forever — the
crawler had no way to know, and no way to answer "which URLs did we
actually confirm contained this target movie" without an engineer manually
joining two Redis namespaces by hand.

This phase adds the reverse-direction half of the bridge: a consumer of
the fingerprinter's own `fingerprint:results:stream:{priority}`, plus one
small, additive extension to the fingerprinter's atomic result-commit
script (sibling repo) so `target_id`/`target_version` → matched `job_id`s
is an O(log n) ZSET lookup instead of a full keyspace scan. Neither
system's core contract, matching algorithm, thresholds, or queue schema
was changed. The original crawler-side claim-token CAS
(`complete_fingerprint_job`) was left untouched — a new, narrower CAS was
added alongside it, not a modification to it (see §4).

## 2. Existing contracts inspected (read-only, both repos, before writing anything)

**Crawler (this repo):** `storage/media_evidence_store.py` (the
`MediaEvidenceStore` protocol, `FingerprintResult`, `JOB_FORWARDED`'s
docstring — which already named this exact deferred consumer),
`storage/redis_media_evidence_store.py` (`_mark_forwarded_script` —
confirmed it deletes the claim record at forward time, `_complete_script`,
`_encode_result_fields`), `storage/sqlite_media_evidence_store.py`,
`bridge/crawler_fingerprinter_bridge.py`, `bridge/fingerprint_stream_adapter.py`
(the Phase 4 forward-direction adapter this phase's adapter mirrors in
reverse), `bridge/main.py`, `core/config.py`, `core/crawler_manager.py`,
`tests/bridge_test.py`, `tests/redis_media_evidence_store_test.py`.

**Fingerprinter (sibling repo, read-only for everything except the one
change in §5):** `work_queue/keys.py`, `work_queue/results.py`
(`Result`/`ResultRecord`/`ResultDecision`), `work_queue/jobs.py`,
`worker/fingerprint_worker.py` (`Worker.commit_result`,
`_COMMIT_RESULT_IF_CURRENT`), `worker/matching_handler.py`,
`matching/aggregation.py` (`combine`, `temporal_match_to_evidence` — what
actually populates `Result.evidence`), `integration/outcome.py`
(`resolve_outcome` — confirmed zero production callers, test/benchmark
only), `tests/test_results.py`, `tests/conftest.py`.

An audit immediately preceding this phase (not written up as a separate
doc; see conversation record) traced the full pipeline against live Redis
and proved, from source and from running state, that: (a) the fingerprinter
durably records a rich result (`fingerprint:job:{job_id}:result`,
including a full per-technique `evidence` JSON with matched-segment
counts, coverage counts, similarities, temporal offset); (b) nothing
consumed `fingerprint:results:stream:*` in production; (c) `evidence:result:
{aid}` had zero keys and `evidence:events:confirmed_match` did not exist,
confirming `complete_fingerprint_job()` had never been called outside
tests and the manual `--complete-fingerprint-job` CLI flag.

## 3. Architecture decision

**The consumer lives in this repo** (`bridge/` package), as a second,
separate class/process from `CrawlerFingerprinterBridge` — not merged into
it. Three reasons:

1. **The write side can only live here.** Completing a forwarded job means
   mutating `evidence:job:{aid}`/`evidence:result:{aid}`/`evidence:events:
   confirmed_match` — this repo's own schema, own Lua scripts, own
   `RedisMediaEvidenceStore`. There is no way to do this correctly from
   the fingerprinter repo without either a cross-repo Python import
   (forbidden, same rule Phase 4 already established) or hand-replicating
   this repo's own write contract over there — strictly worse than doing
   the write where the schema is actually owned.
2. **The read side needs the exact same posture Phase 4 already
   established for the opposite direction**: `bridge/fingerprint_stream_
   adapter.py` reads nothing from the fingerprinter repo's Python, only
   replicates its Redis field/key contract by hand, with an explicit
   "must be updated by hand if the fingerprinter contract ever changes"
   disclaimer. `bridge/fingerprint_result_adapter.py` (new, §6) is the
   same pattern, same disclaimer, mirrored in reverse.
3. **Separate responsibility, separate class.** `CrawlerFingerprinterBridge`'s
   own docstring and tests model one direction only (claim → validate →
   forward). A second class (`FingerprintResultConsumer`) with its own
   process entrypoint (`bridge/result_consumer_main.py`, mirroring
   `bridge/main.py`'s framing exactly) keeps that boundary clean while
   reusing the same package, same deployment convention
   (`crawler/env/bin/python3 -m bridge.result_consumer_main`), same
   `RedisMediaEvidenceStore`.

**The per-target match index lives in the fingerprinter repo**, as an
additive extension to the *already-atomic* `commit_result` script — not a
separate index-maintenance consumer. The fingerprinter is the authoritative
owner of `target_id`/`target_version`/`decision`; writing the index in the
same atomic script that commits the result means the index can never
desync from the result it indexes (either both are written, or — on a
stale/CAS-rejected attempt — neither is), with zero new moving parts, zero
new consumer group, zero eventual-consistency window.

## 4. The claim-token question — investigated first, not assumed

`mark_fingerprint_job_forwarded`'s Lua script
(`storage/redis_media_evidence_store.py`) does `ZREM jobs:inflight` and
**`DEL claim_key`** the moment a job becomes `forwarded` — the claim
record holding the original token is gone by design (ownership
deliberately transferred to the fingerprinter at hand-off; `JOB_FORWARDED`'s
own docstring already said as much). `complete_fingerprint_job`'s CAS
checks `HGET claim_key token == given token`; since `claim_key` no longer
exists post-forward, that check **cannot succeed again for a forwarded
job, by construction** — confirmed by a new test,
`TestCompleteForwardedFingerprintJob::test_claim_token_cannot_be_used_to_
complete_a_forwarded_job`.

Reusing that token, or weakening its CAS, was never on the table. Instead,
a **new, separate** method — `complete_forwarded_fingerprint_job(asset_id,
*, fingerprint_job_id, result)` — is gated on a *different* fact that
`mark_fingerprint_job_forwarded` itself durably recorded at hand-off time:

```lua
if job.status ~= 'forwarded' or job.fingerprint_job_id ~= given_fingerprint_job_id then
    return 'stale'
end
```

Two properties fall out of this for free:

- **Idempotent.** Once a result is recorded, `status` is no longer
  `forwarded`, so a redelivered/duplicate result event is a safe no-op —
  no separate dedup bookkeeping needed.
- **Integrity-preserving.** A result event correlated to the wrong asset
  (a bug, or a stale/misrouted event) can never overwrite this asset's
  evidence — `fingerprint_job_id` must match exactly what this asset was
  actually forwarded as.

The original token CAS (`complete_fingerprint_job`, used only by the
manual `--complete-fingerprint-job` CLI path) is **completely untouched** —
same method, same signature, same behavior, zero lines changed.

## 5. Fingerprinter repo change (the only change there)

`worker/fingerprint_worker.py`'s `_COMMIT_RESULT_IF_CURRENT` Lua script
gained one `KEYS` entry and four lines:

```lua
if result_fields.decision == 'match' then
    redis.call('ZADD', KEYS[5], ARGV[4], result_fields.job_id)
end
```

`KEYS[5]` is `work_queue.keys.match_index_key(target_id, target_version)`
(new function: `fingerprint:matches:target:{target_id}:{target_version}`),
computed in Python from the already-known `job.target_id`/`job.target_version`
and passed alongside the four keys the script already used. `ARGV[4]` is
`completed_at`, already computed for the state/result writes — no new
argument. Gated on the exact same attempt-fencing CAS as every other write
in this script. `Result`/`ResultRecord`'s schema is unchanged; no new
field, no `RESULT_SCHEMA_VERSION` bump.

## 6. Redis contracts — new and reused

| Key | Repo | Kind | Producer | Consumer | Notes |
|---|---|---|---|---|---|
| `fingerprint:matches:target:{target_id}:{target_version}` | fingerprinter | ZSET (member=`job_id`, score=`processing_completed_at`) | `Worker.commit_result`, MATCH only | none yet (introspection/future dashboard) | New. Unbounded retention, matching `fingerprint:job:*:result`'s existing convention — no new retention policy invented. |
| `fingerprint:results:stream:{priority}` | fingerprinter | Stream | `Worker.commit_result` (pre-existing) | **`FingerprintResultConsumer`, new** (consumer group `crawler-evidence-consumers`, configurable) | Existing key, newly consumed. Distinct group from the fingerprinter's own `fingerprinter-workers` (different stream). |
| `fingerprint:job:{job_id}:result` | fingerprinter | Hash | `Worker.commit_result` (pre-existing) | `FingerprintResultConsumer` (read, via `bridge/fingerprint_result_adapter.py`'s replica) | Read-only from this repo; single authoritative source for confidence/algorithm/evidence/target_version. |
| `evidence:job:{aid}` | crawler | Hash | `mark_fingerprint_job_forwarded` (pre-existing) | `complete_forwarded_fingerprint_job` (read `status`/`fingerprint_job_id` for the CAS) | Existing key, new reader/writer transition (`forwarded` → `completed`). |
| `evidence:result:{aid}` | crawler | Hash | `complete_forwarded_fingerprint_job`, **new caller** | `list_media_assets`, any future reader | Existed in code (Phase 1), never reached in production before this phase. Now carries `evidence` (new field, §7) alongside the pre-existing fields. |
| `evidence:events:confirmed_match` | crawler | Stream | `complete_forwarded_fingerprint_job`, **new caller**, MATCH only | none yet (§19 domain-scoring consumer remains a separate future item) | Same as above — existed, unreached, now reached. |

No Redis namespace, DB index, or queue contract was added or changed
beyond the one new ZSET above.

## 7. `FingerprintResult` — one additive field

`storage/media_evidence_store.py`: `evidence: Optional[str] = None`,
appended after `processed_at`, default `None` — every existing
constructor call (CLI, tests) is unaffected. Carries the fingerprinter's
own per-technique `evidence` JSON verbatim (matched-segment counts,
coverage counts, similarities, temporal offset — everything `matching.
aggregation.temporal_match_to_evidence` puts there), so a reader of this
store alone — zero access to the fingerprinter's own Redis namespace — has
the full matching evidence for a confirmed match. Threaded through
`RedisMediaEvidenceStore._encode_result_fields` and a new nullable
`evidence TEXT` column on `SQLiteMediaEvidenceStore`'s `fingerprint_results`
table (same no-migration `CREATE TABLE IF NOT EXISTS` convention already
used for that table's other columns).

**Field mapping** (fingerprinter `Result`/`ResultRecord` →
`FingerprintResult`, in `bridge/fingerprint_result_consumer.py`):

| fingerprinter | → | crawler |
|---|---|---|
| `decision="match"` | → | `aggregate_decision="confirmed"` |
| `decision="no_match"` | → | `aggregate_decision="rejected"` |
| `decision="processing_failure"` | → | `aggregate_decision="uncertain"` |
| `confidence` | → | `confidence` |
| evidence entry, `technique=="dinov2"` → `score` | → | `dinov2_similarity` |
| evidence entries' `{technique: matcher_version}` | → | `algorithm_versions` |
| `worker_id` | → | `worker_id` |
| `processing_completed_at` | → | `processed_at` (stringified) |
| full `evidence` JSON, verbatim | → | `evidence` |
| — | | `matched_title` left `None` (fingerprinter has no title concept) |

**All three decisions** are propagated (not just `MATCH`) — the crawler's
own `derive_asset_status()`/`JOB_COMPLETED` display logic already exists
to show the real terminal state; leaving `no_match`/failed candidates
stuck at `forwarded` forever would be a worse, misleading state than
recording the truth. **Only `MATCH`** triggers the `confirmed_match`
stream event and the fingerprinter-side match index — unchanged from how
`complete_fingerprint_job`'s existing script already gated the event.

## 8. New files (crawler repo)

- **`bridge/fingerprint_result_adapter.py`** — Redis-native replica of the
  fingerprinter's result schema (`result_key`, `results_stream_key`,
  `ResultDecision`, `parse_result_event`, `parse_result_record`,
  `ResolvedResult`), same hand-maintained-replica posture as
  `fingerprint_stream_adapter.py`. Only `job_id` is trusted from the
  stream event itself; every other field (decision, confidence, evidence,
  target_version, ...) is read from the authoritative `result_key(job_id)`
  hash — the event is deliberately "just enough to find the record," per
  the fingerprinter's own `to_event_fields()` docstring.
- **`bridge/fingerprint_result_consumer.py`** — `FingerprintResultConsumer`:
  `XREADGROUP`/`XAUTOCLAIM`/`XACK` against
  `fingerprint:results:stream:{priority}` (mirrors `Worker.claim_one`/
  `reclaim_stale` from the fingerprinter almost exactly — the same Redis
  Streams consumer-group idiom, third occurrence across the two repos, all
  hand-kept in sync per the established cross-repo-replica convention),
  `ResultConsumerMetrics` (mirrors `BridgeMetrics`'s plain-dataclass style).
- **`bridge/result_consumer_main.py`** — process entrypoint, mirrors
  `bridge/main.py` (argparse, SIGTERM/SIGINT → graceful `stop()`,
  `build_media_evidence_store`, Redis-backend-only enforcement).

## 9. Configuration

New, additive-only `core.config.ResultConsumerConfig` (`config.yaml` not
modified — every field has a default):

```yaml
crawler:
  result_consumer:
    consumer_group: crawler-evidence-consumers
    priorities: [default]     # see §12 — matches worker/main.py's actual behavior
    block_ms: 5000
    lease_ms: 30000
    reclaim_batch_size: 10
    reclaim_interval_seconds: 60.0
    idle_sleep_seconds: 2.0
```

Run as its own process: `crawler/env/bin/python3 -m bridge.result_consumer_main
[--config config.yaml] [--consumer-name result-consumer-1] [--once]`.
Requires the Redis media-evidence backend (fails clearly at startup
otherwise — the fingerprinter's result stream this consumer reads only
exists in Redis).

## 10. Observability

`loguru`, this repo's existing convention, matching `CrawlerFingerprinterBridge`'s
style: `result-consumer: received ...` (DEBUG), `result-consumer: MATCH
recorded ...` / `NO_MATCH recorded ...` / `PROCESSING_FAILURE recorded ...`
(INFO), `result-consumer: malformed result event/record ...` (WARNING),
`result-consumer: cannot resolve crawler evidence ...` (WARNING),
`result-consumer: stale/duplicate completion ...` (WARNING),
`result-consumer: infrastructure failure, backing off ...` (ERROR), and a
final `result-consumer: shutdown metrics=...` line. `ResultConsumerMetrics`
tracks `results_claimed`/`matches_recorded`/`no_matches_recorded`/
`failures_recorded`/`stale_skipped`/`malformed_rejected`/`evidence_missing`.

## 11. Tests

**Fingerprinter repo** — `tests/test_results.py`, 7 new tests: MATCH
creates the index entry; score equals `processing_completed_at`; NO_MATCH
and `processing_failure` create no entry; two MATCHes for the same target
both appear; a MATCH for target A is invisible under target B; a stale
(reclaimed-then-superseded) attempt's index write is rejected exactly like
its result/state/event write already was.

**Crawler repo** — `tests/redis_media_evidence_store_test.py`, new
`TestCompleteForwardedFingerprintJob` (9 tests): matching
`fingerprint_job_id` completes the job and the raw `evidence` JSON
round-trips; MATCH emits `confirmed_match`; `rejected`/`uncertain` do not;
wrong `fingerprint_job_id` is rejected as stale without touching evidence;
duplicate completion is a no-op (exactly one `confirmed_match` event); a
never-forwarded or unknown asset id cannot be completed; the original
claim-token path still (correctly) cannot complete a forwarded job.

`tests/fingerprint_result_consumer_test.py`, new, 11 tests: MATCH recorded
and correlated; full provenance (candidate URL, source domain, decision,
confidence, evidence JSON) recoverable from `evidence:*` alone; redelivered
duplicate is a safe no-op; NO_MATCH and `processing_failure` persisted
without a `confirmed_match` event; wrong-`fingerprint_job_id` mismatch does
not overwrite evidence; missing crawler evidence is handled explicitly
(logged, acked, not retried forever, no crash); malformed stream event and
missing result hash are rejected and acked, not retried forever; a
crashed/never-acked entry is recovered exactly once via `reclaim_stale()`
(`XAUTOCLAIM`); an infrastructure failure (`MediaEvidenceUnavailable`)
leaves the entry unacked, still pending.

## 12. Test commands and results — TESTED

```text
$ fingerprinter/env/bin/python -m pytest tests/ -q --ignore=tests/test_integration_e2e.py
428 passed in 62.80s

$ fingerprinter/env/bin/python -m pytest tests/test_integration_e2e.py -q
7 passed in 13.48s
```

**Fingerprinter full suite: 435 passed, 0 failed.**

```text
$ crawler/env/bin/python -m pytest tests/redis_media_evidence_store_test.py -q
35 passed in 0.34s

$ crawler/env/bin/python -m pytest tests/fingerprint_result_consumer_test.py -q
11 passed in 0.39s

$ crawler/env/bin/python -m pytest tests/ -q --ignore=tests/benchmarks
418 passed, 3 skipped in 139.33s
```

**Crawler full suite: 418 passed, 3 skipped, 0 failed.**

Five fingerprinter subprocess tests (`test_worker_observability.py`,
`test_worker_main.py`) failed once when run concurrently with other
background load in this session (a `python -m worker.main` subprocess
exceeding a 10s startup timeout — cold model load contending for CPU).
Reproduced via `git stash`/`stash pop`: the same failure occurred with
this phase's changes fully reverted, and both the isolated re-run and the
full-suite re-run (numbers above) passed cleanly. **Confirmed pre-existing
environment timing sensitivity, unrelated to this phase.**

## 13. Real Redis validation — VERIFIED

Isolated, confirmed-empty Redis DB 2 throughout (never DB 0 — the live
`worker.main`/`bridge.main` processes from this session's earlier manual
testing were running against DB 0 the entire time and were never touched,
confirmed running, unaffected, both before and after). Real code, three
separate process invocations (one per repo's own environment,
communicating purely through Redis — the same topology production uses):

1. **Crawler env**: registered `blast`/`v1` in an isolated `fingerprint:
   target:*` key, then a real `RedisMediaEvidenceStore` + a real
   `CrawlerFingerprinterBridge.process_one()` (×2) forwarded two
   candidates — one destined to MATCH, one to NO_MATCH — onto
   `fingerprint:jobs:stream:default`. Confirmed `evidence:job:{aid}.status
   == "forwarded"` for both, real derived `fingerprint_job_id`s recorded.
2. **Fingerprinter env**: a real `Worker(priority="default")` claimed both
   real forwarded jobs and called the real (patched) `commit_result` with
   synthetic `Result` objects (`decision=MATCH` for one, `NO_MATCH` for
   the other — no DINOv2 inference run; the matching algorithm itself was
   already validated manually per this phase's preceding audit, and this
   step's purpose was the plumbing, not the model). Confirmed
   `fingerprint:matches:target:blast:v1` contained **exactly** the MATCH
   job's `job_id` — not the NO_MATCH job's.
3. **Crawler env**: a real `FingerprintResultConsumer` (fresh instance,
   own consumer group) drained `fingerprint:results:stream:default` (2
   entries). Confirmed final state: MATCH asset `status="confirmed"`,
   `match_confidence=0.9366`, exactly one `confirmed_match` event with the
   correct `source_domain`; NO_MATCH asset `status="rejected"`, no event.
   Printed the full provenance record recoverable from `evidence:*`
   alone — candidate URL, source domain, `media_evidence_id`, decision,
   confidence, worker_id, timestamp, and the complete match-evidence
   dict (`matched_segment_count=4`, `total_target_segments=1699`,
   `total_candidate_segments=12`, `mean_similarity=0.9366`,
   `coarse_similarity=0.7816`, `temporal_offset_s=650.0`) — with **zero**
   `fingerprint:*` reads in that final step.

DB 2 flushed back to empty afterward (`redis-cli -n 2 FLUSHDB`, confirmed
0 keys before and after); DB 0 key count and both live processes confirmed
unaffected throughout.

## 14. Limitations

- **No historical backfill.** Results already committed before this phase
  (the two `manual-test-001` MATCHes and six real `no_match` results
  observed live during the preceding audit) are not retroactively indexed
  or pushed through the new consumer — only results committed going
  forward, by a worker process running this phase's code.
- **The live `worker.main`/`bridge.main` processes from this session's
  manual testing are still running the pre-phase code in memory** — a
  long-running Python process does not pick up a Lua-script-string change
  without a restart. Restarting them was correctly treated as the
  operator's decision, not made unilaterally here.
- **`worker/main.py` still has no priority-stream selection** (Phase 4 §20
  already noted this) — `ResultConsumerConfig.priorities` defaults to
  `["default"]` to match that reality; `high`/`low` results will not be
  consumed until that separate, pre-existing gap is closed (out of this
  phase's scope, explicitly not touched).
- **The result consumer is not yet deployed as a running process.**
  `python -m bridge.result_consumer_main` exists, is tested, and was
  validated end-to-end (§13) — nothing currently runs it continuously in
  this environment.
- **No cross-repo drift detection beyond tests actually being run.** Same
  caveat Phase 4 already carries for `fingerprint_stream_adapter.py`:
  if the fingerprinter's `Result`/`ResultRecord`/key conventions change,
  `bridge/fingerprint_result_adapter.py` must be updated by hand.
- **The per-target match index has no retention/trim policy** — same,
  deliberate, unbounded-growth posture `fingerprint:job:*:result` already
  has; not a new problem this phase introduced, not solved by it either.
- **`evidence:events:confirmed_match` still has no real consumer.**
  Phase 1's §19 domain-scoring consumer remains a separate, still-deferred
  item — this phase makes the stream *reachable in production* for the
  first time, it does not add a reader for it.

## 15. Future work

1. A domain-scoring (or other) consumer of `evidence:events:confirmed_match`
   — Phase 1's original §19 item, still open.
2. A dashboard/reporting surface over `fingerprint:matches:target:{id}:
   {version}` + `evidence:result:{aid}` + `evidence:asset:{aid}` — the
   backend state this phase produces is now sufficient to answer "all
   confirmed matches for target X/version Y" and "why was this URL
   classified as piracy" without re-running the crawl/fingerprint
   pipeline; no UI exists yet (explicitly out of this phase's scope).
   Both the audit that preceded this phase and this phase's own real
   Redis validation (§13) prove the underlying data is present and
   correctly shaped — building the surface on top is a separate,
   additive project.
3. Priority-stream selection for `worker/main.py` (fingerprinter repo),
   which would let `ResultConsumerConfig.priorities` cover `high`/`low`
   meaningfully — same pre-existing gap Phase 4 named, still unowned.
4. Historical backfill for results committed before this phase (§14) —
   would need to re-derive which pre-existing `fingerprint:job:*:result`
   entries correspond to still-`forwarded` `evidence:job:{aid}` records
   and complete them; not attempted here.
5. Deploying `bridge/result_consumer_main.py` as a running, supervised
   process alongside the existing `bridge.main`/`worker.main` processes.

## 16. Exact files changed

**Fingerprinter repo** (one change, additive):
- `work_queue/keys.py` (+`match_index_key`)
- `work_queue/__init__.py` (+export)
- `worker/fingerprint_worker.py` (+1 `KEYS` entry, +4 Lua lines, +1 Python
  arg in `commit_result`)
- `tests/test_results.py` (+7 tests)

**Crawler repo:**

New:
- `bridge/fingerprint_result_adapter.py`, `bridge/fingerprint_result_consumer.py`,
  `bridge/result_consumer_main.py`
- `tests/fingerprint_result_consumer_test.py`
- `docs/architecture/phase-5-fingerprint-result-consumer.md` (this file)

Modified (additive only):
- `storage/media_evidence_store.py` (+`evidence` field, +protocol method)
- `storage/redis_media_evidence_store.py` (+Lua script, +method, +`evidence`
  in `_encode_result_fields`)
- `storage/sqlite_media_evidence_store.py` (+column, +method, parity)
- `core/config.py` (+`ResultConsumerConfig`, attached as
  `CrawlerConfig.result_consumer`)
- `tests/redis_media_evidence_store_test.py` (+`TestCompleteForwardedFingerprintJob`,
  9 tests)
- `docs/architecture/system-architecture.md` (§21 corrected — see that
  file's own diff; the "no process reads this stream" claim was true when
  written, no longer is)

`config.yaml` was **not modified** in either repo — every new field has a
default. The original claim-token CAS (`complete_fingerprint_job`) was
**not modified** — zero lines changed in that method.

## 17. Final phase status

**Phase 5 is complete.** A real fingerprint result — committed by a real
`Worker.commit_result` call, through the real atomic CAS script — now
travels through `fingerprint:results:stream:{priority}` → a real
`FingerprintResultConsumer` → `evidence:result:{aid}` /
`evidence:job:{aid}.status` / `evidence:events:confirmed_match` (MATCH
only), with the fingerprinter's full per-technique match evidence
preserved verbatim, target identity (`target_id`+`target_version`)
preserved, and idempotent/crash-safe/integrity-checked delivery — proven
against real local Redis with real `Worker`/`CrawlerFingerprinterBridge`/
`FingerprintResultConsumer` instances, not mocks alone. The system can now
answer "which URLs did we confirm contained target X/version Y, and why"
from durable state alone, without re-running the crawl or fingerprint
pipeline. No dashboard/reporting UI was built (§15, item 2) — that remains
a separate, future, additive project.
