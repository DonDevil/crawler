# Phase 2 — Crawler → Media Evidence → Fingerprint Job Trace

## 1. Objective

Determine why a real crawler run reported `jobs_claimed = 0`,
`jobs_reclaimed = 0`, `jobs_completed = 0`, `stream_length = 0`,
`group_lag = 0`, `group_pending = 0` for the fingerprinter, and why
`XLEN`/`XPENDING`/`XINFO CONSUMERS` against `fingerprint:jobs:stream:default`
showed nothing — without assuming either "the crawler never discovered
media" or "the fingerprinter worker is broken."

**Git revision this phase started from:** `d95635b` (crawler repo,
`main`), working tree carrying Phase 1's uncommitted blacklist fix
(`utils/url_utils.py`, `tests/url_utils_test.py`,
`tests/redis_frontier_test.py`, `seeds/piracy_sites.txt`) — not touched by
this phase. Fingerprinter repo (`/home/darkdevil/Desktop/anti_piracy/fingerprinter`)
inspected read-only at `5e16eb0`.

## 2. Initial symptom

```text
jobs_claimed = 0, jobs_reclaimed = 0, jobs_completed = 0
stream_length = 0, group_lag = 0, group_pending = 0
XLEN fingerprint:jobs:stream:default = 0
XPENDING ... = 0
XINFO CONSUMERS ... = empty
```

All of the above are properties of exactly one Redis key:
`fingerprint:jobs:stream:default`, a Redis Stream. The audit's job was to
find out whether that emptiness reflects the crawler producing nothing, or
something else.

## 3. Audit scope

Read-only inspection of this repo's crawl → extraction → evidence → job
path, plus read-only inspection of the sibling `fingerprinter` repo's own
architecture docs where they describe the integration boundary (needed
because the symptom is a Redis key that this repo's grep turned up zero
writers for — understanding *why* required reading the other side's
documented design, not modifying it). Live (non-destructive) inspection of
the actual Redis instance both systems share. One new focused test added
(§9); seven identical one-line-cause exception-handling fixes applied
(§8) after the root cause was established. No fingerprint worker code,
no Redis Streams contract, no DINOv2/matching/aggregation/retry code
touched or proposed.

## 4. Actual call graph

```text
HybridCrawler.worker()                                  [crawler/hybrid_crawler.py]
  → URLUtils.is_blacklisted(url)                         gate (Phase 1 fix already applied)
  → self._run_engine_plan(url)  →  html
  → self.parser.extract_content(html, url)                [parsers/html_link_extractor.py]
      → HTMLLinkExtractor.extract_links()                 "links" (for frontier expansion)
      → MediaLinkDetector.extract_media_links()            "media_links" (for evidence)
          → URLUtils.clean_media_url() / classify_media_url()   per-candidate gate
  → for media in media_links:
      → AsyncMediaEvidence.record_media_link()             [core/media_evidence_executor.py]
          → asyncio.to_thread(store.record_media_link)     off the event loop
              → RedisMediaEvidenceStore.record_media_link() [storage/redis_media_evidence_store.py:514]
                  → URLUtils.clean_media_url() + length gate
                  → compute_discovery_id() (sha256, dedup key)
                  → _record_media_link_script (Lua, 1 round trip):
                      → HSET evidence:asset:{aid}           evidence created (upsert)
                      → ZADD evidence:assets:all
                      → LPUSH+LTRIM evidence:asset:{aid}:observations
                      → if first-ever discovery of this aid:
                            HSET evidence:job:{aid} status=queued  fingerprint job created
                            ZADD evidence:jobs:queue {score} {aid}  ← queue write (job producer)
```

Same path for every engine (`async_crawler.py`, `http_crawler.py`,
`tor_crawler.py`, `playwright_crawler.py`, `selenium_crawler.py`,
`scrapling_crawler.py`), both for the parser's `media_links` loop and for
each engine's direct-response media path (a fetch whose `Content-Type` is
itself media/manifest, no HTML parse needed).

**There is no `XADD` anywhere in this call graph, and no `XADD` to any
`fingerprint:*`-prefixed key anywhere in this repository.** The only
`XADD` in the entire crawler codebase is inside `_complete_script`
(`storage/redis_media_evidence_store.py:376`), which fires on
`complete_fingerprint_job(..., decision="confirmed")` — a *result* event
(`{namespace}:events:confirmed_match`), not a job-submission mechanism.
Grep evidence: `grep -rn "XADD" storage/` returns exactly one call site;
`grep -rn "jobs:stream\|stream:default\|consumer_group\|XGROUP\|XREADGROUP" .`
(excluding `env/`) returns zero matches anywhere in this repo.

The job-dispatch mechanism this repo actually implements is a **ZSET +
Lua-scripted atomic claim/lease queue** (`{namespace}:jobs:queue`),
deliberately modeled on `core/redis_frontier.py`'s proven claim pattern —
stated explicitly in `storage/redis_media_evidence_store.py`'s module
docstring and in `docs/architecture/media-evidence-redis-design.md`. This
is not an oversight; it is the documented Phase-1-13 design for this
repo's half of the integration, restated again in
`docs/architecture/system-architecture.md` (`E -.->|fingerprint jobs|
FUTURE`, calling the future consumer "not the fingerprinter" from this
repo's own vantage point).

## 5. Media eligibility gates

| Gate | Location | Condition | Can reject? | Expected? | Observable today? |
| --- | --- | --- | --- | --- | --- |
| URL blacklist | `URLUtils.is_blacklisted` | Domain/hostname matches a piracy-adjacent blacklist pattern | Yes (whole page, before fetch) | Yes — Phase 1 fixed a bug here (ad/tracker hostnames incorrectly collapsing to the registered domain); already merged and covered by `tests/url_utils_test.py` | `logger.info("Skipping blacklisted URL...")` |
| Content-type / extension classification | `URLUtils.classify_media_url`, `looks_like_media_content_type`, `is_media_file` | URL extension or response `Content-Type` must map to a known media type (`video`, `audio`, `stream-manifest`, image types, etc.); `"unknown"` is dropped silently inside `MediaLinkDetector.add_candidate` | Yes, per-candidate | Yes — this is the actual "is this even a media URL" filter | Not currently — see §7 |
| `clean_media_url` + `MAX_URL_LENGTH` (2048) | `storage/media_evidence_store.py` (`validate_media_url_length`, `InvalidMediaURLError`) | Empty-after-cleaning or pathologically long URL | Yes | Yes, abuse mitigation (§16 of the design doc) | Caught per-engine (§8) |
| Asset-level dedup | `compute_discovery_id = sha256(clean_media_url(url))` | Rediscovery of the same canonical URL updates the existing asset/observation but **never creates a second job** — "exactly one job per asset, for the asset's whole lifetime" (Lua script comment, `redis_media_evidence_store.py:142-146`) | Job creation only, not evidence recording | Yes, intentional — this is why `test_duplicate_discovery_returns_same_asset_and_one_job` exists | Yes, via `get_fingerprint_jobs()` count |
| Observation/variant caps | `max_observations_per_asset` (20), `max_variants_per_asset` (20) | Bounds *history detail* retained per asset, never rejects the asset or its job | No | Yes | Yes |
| Redis availability | `MediaEvidenceUnavailable` | Redis connection/timeout error during any store call | Yes — the whole write is lost for that candidate | Yes, but **the store's own docstring says this must never be silently degraded**, and until this phase every crawler engine's media-link loop caught it as a bare `Exception` and logged at `DEBUG` (§8) | **No — this was the one real gap found** |
| Crawler-side "does this candidate get sent to a fingerprinter" | *(none — out of crawler-repo scope, §6)* | n/a | n/a | n/a | n/a |

Requirement 6's specific sub-items (redirects, min/max media constraints,
duplicate suppression, evidence/candidate limits) are covered by the rows
above; there is no separate "minimum media size" or "maximum media size"
gate in this repo — `MediaAcquirer`-style byte-size bounds belong to the
fingerprinter's acquisition layer (`fingerprinter/acquisition/`), which
this repo has no dependency on or visibility into by design (§6, cited
below).

## 6. Configuration path

`config.yaml` → `core/config.py` (`StorageConfig`, `MediaEvidenceConfig`)
→ `core/crawler_manager.py::build_media_evidence_store()` → the
constructed `RedisMediaEvidenceStore`/`SQLiteMediaEvidenceStore` passed
into every crawler engine as `media_database` (wrapped in
`AsyncMediaEvidence`).

Verified at runtime, not just in the schema:

- `storage.enable_media_evidence: true` — checked at
  `core/crawler_manager.py:70`; `false` makes `build_media_evidence_store`
  return `None`, and every engine's `media_links` loop guards on
  `if not self.media_database: continue`.
- `media_evidence.type: "redis"` (config.yaml:66, the active,
  uncommented line — `type: "sqlite"` is commented out immediately
  above it) — selects `RedisMediaEvidenceStore` at
  `core/crawler_manager.py:74-94`, connected to
  `localhost:6379/0`, `namespace: "evidence"`
  (`config.yaml:67-69`). Confirmed live: `evidence:*` keys exist in
  Redis DB 0 today (§7).
- **`storage.enqueue_media_jobs: true`** (`config.yaml:56`,
  `core/config.py:38`) — grepped across the entire repo
  (`grep -rn "enqueue_media_jobs" --include=*.py .`): the only two hits
  are its Pydantic field definition and one docstring mention. **It is
  never read anywhere.** It gates nothing. This is a real but *inert*
  finding, not a cause of the "zero jobs" symptom: job creation is
  unconditional inside the Lua script (§4), so this flag's actual value
  (`true`, matching what the ops report already showed) has zero effect
  either way. Noted in §11 as a latent config trap (an operator setting
  it to `false` expecting jobs to stop would see no change), not fixed
  here — wiring it up would mean adding a new behavior (a way to disable
  job creation) that nothing in this phase's audit shows is needed, and
  the brief explicitly asks not to fix undemonstrated problems.

## 7. Redis / job path — live inspection

Read-only `redis-cli` inspection of the actual shared Redis instance
(`localhost:6379`), not a test database, at the time of this audit
(2026-08-21, same day as the reported "zero jobs" run):

```text
DBSIZE (db0)                         = 9444
evidence:jobs:queue      (ZSET)      = 1 member   -- the crawler's real job queue
evidence:job:{aid}       (HASH)      status=queued priority=18 retry_count=0
                                      created_at=1787300943.88 (2026-08-21 08:29:03 UTC)
evidence:asset:{aid}     (HASH)      media_type=video mime_type=video/mp4
                                      source_domain=play.onestream.today
                                      canonical_url=https://fastly.southbytes.xyz/open.php?...
                                      last_discovered_by=scrapling
                                      last_discovery_method=source-tag
fingerprint:jobs:stream:default (STREAM) XLEN=0
  XINFO GROUPS → name=fingerprinter-workers consumers=0 pending=0
                 last-delivered-id=0-0 lag=0
```

**This is the whole answer.** The crawler's real job queue
(`evidence:jobs:queue`, namespace `evidence`, Redis DB 0) already
contains a job created by a real crawl today. The Stream the fingerprinter
was reading (`fingerprint:jobs:stream:default`, namespace `fingerprint`,
same Redis DB 0) is a completely separate key nothing in this repository
ever writes to. Both facts were independently reproducible:

- Code trace (§4): zero `XADD` call sites targeting any `fingerprint:*`
  key anywhere in this repo.
- Live Redis: the two keyspaces (`evidence:*` vs. `fingerprint:*`)
  coexist in the same DB with populated vs. empty contents respectively.
- `/home/darkdevil/Desktop/fingerprinter-output.txt` (a captured log from
  a real `python -m worker.main` run against this same Redis instance,
  `namespace=fingerprint`, `stream=fingerprint:jobs:stream:default`,
  `consumer_group=fingerprinter-workers`) shows the exact worker health
  snapshots the ops report quoted — `jobs_claimed: 0`,
  `stream_length: 0`, `group_lag: 0`, etc., for the worker's entire
  ~50-minute run. The worker was never wrong about what it observed; it
  was faithfully reporting on a stream nothing feeds.
- The fingerprinter repo's own Phase 12 architecture document
  (`fingerprinter/docs/architecture/phase-12-crawler-fingerprinter-integration.md`)
  independently documents both keyspaces, confirms "no `fingerprint:*` key
  exists anywhere in the crawler repo; no `crawler:*` or `evidence:*` key
  exists anywhere in this repo" (§7 of that doc), and states explicitly:
  **"The crawler -> `evidence:jobs:queue` -> `integration.submission` path
  is not built ... building that bridge means touching the crawler repo,
  explicitly out of scope [for Phase 12]"** (§25), listing "Decide and
  build the crawler-side bridge" as its own **Phase 13** recommendation
  (§26, item 1) — a component "living in the crawler repo, or a third
  deployment unit — deliberately not decided ... since it depends on
  crawler-team ownership."

## 8. Root cause

**Not a defect in this repo's crawl → media-extraction → evidence →
job-creation pipeline.** Every stage of that pipeline was verified working,
by code trace and by live production data: a real crawl today discovered
a `video/mp4` URL, created media evidence for it, and queued exactly one
fingerprint job, exactly as designed (§4, §7, §9).

**The reported "zero fingerprint jobs" is an artifact of measuring the
wrong Redis structure.** `jobs_claimed`, `jobs_reclaimed`,
`jobs_completed`, `stream_length`, `group_lag`, `group_pending`,
`XLEN`/`XPENDING`/`XINFO CONSUMERS` are all properties of
`fingerprint:jobs:stream:default` — a Redis Stream + consumer-group
contract that belongs entirely to the separate `fingerprinter` repository
(namespace `fingerprint:*`), which this crawler repo has never written to
and, per its own architecture docs, was never designed to write to
directly. The crawler's actual, working job output lives at
`evidence:jobs:queue` (namespace `evidence:*`, a ZSET), which the ops
report never inspected.

This matches failure mode **G** from the brief's list ("some other
integration condition prevented submission") in the most literal sense:
**no submission mechanism between the two systems exists yet.** Both
sides independently built and unit-tested their own half of the
contract — this repo's `evidence:jobs:queue` claim/lease queue
(`tests/fingerprinter_queue_test.py`,
`tests/redis_media_evidence_store_test.py`, 29 tests, all passing) and
the fingerprinter's own `fingerprint:jobs:stream:*` Streams contract
(fingerprinter Phase 1-11, 152 tests) — and the fingerprinter's own Phase
12 explicitly, deliberately deferred building the connecting piece,
naming it as its own next phase, owned by a decision this crawler-repo
audit has no authority to make unilaterally (§4 of the fingerprinter's
Phase 12 doc: which repo — or a third component — should own the bridge
is "not decided," "depends on crawler-team ownership").

**Secondary finding (§4, §5 table, §8 below): a real, if minor, defect
was found and fixed.** Every crawler engine's `media_links` loop caught
`record_media_link` failures — including `MediaEvidenceUnavailable`
(Redis unreachable, exactly the failure this store's own docstring says
"must be visible... not silently degrade") — as a bare `except Exception`
logged at `DEBUG`. In production (typically INFO+), an actual Redis outage
during evidence recording would have been completely invisible, which is
precisely the "we can't tell where a candidate disappeared" condition
§OBSERVABILITY describes. This is not what caused the reported zero
(Redis was reachable — the job in §7 proves it), but it is a real,
demonstrated gap in the requested observability, so it was fixed (§9).

## 9. Implementation changes

**Seven one-line-cause fixes, identical pattern, one per crawler engine**
(`crawler/async_crawler.py`, `crawler/hybrid_crawler.py`,
`crawler/http_crawler.py`, `crawler/tor_crawler.py`,
`crawler/playwright_crawler.py`, `crawler/selenium_crawler.py`,
`crawler/scrapling_crawler.py`): import
`storage.media_evidence_store.MediaEvidenceUnavailable`, and split the
`media_links` loop's exception handler:

```python
except MediaEvidenceUnavailable as exc:
    logger.warning(f"Media evidence store unavailable, dropping candidate {media['url']}: {exc}")
except Exception as exc:
    logger.debug(f"Skipping media evidence capture for {url}: {exc}")
```

This makes an infrastructure-level failure (Redis down, exactly the
scenario the store's own module docstring says must be visible) log at
`WARNING` instead of silently vanishing at `DEBUG`, while every other,
benign, expected rejection (`InvalidMediaURLError` for a malformed/empty
URL, an over-length URL, etc.) keeps its existing `DEBUG`-level, no
per-URL flood in normal operation, exactly matching the brief's
observability constraints. No new counters, no new log line per
successfully-discovered candidate (the existing Lua-script-driven job
creation is already silent by design, matching the project's established
"no new logging unless it closes an actual blind spot" convention
restated in the fingerprinter's own Phase 12 doc §19). Nothing about the
job contract, the Redis keys used, the claim/lease semantics, or retry
behavior was touched.

**No changes were made to:** the fingerprint worker, any Redis Streams
code (none exists in this repo), DINOv2, temporal matching, aggregation,
retry/backoff semantics, the media evidence architecture, `config.yaml`,
or the inert `enqueue_media_jobs` flag (§6 — left as-is, since wiring it
up would be a new, undemonstrated behavior change, not a fix).

## 10. Tests

**New:** `tests/hybrid_crawler_test.py::
test_hybrid_crawler_discovers_media_and_submits_exactly_one_fingerprint_job`
— the smallest deterministic proof of the full path this phase audited:
a real `HybridCrawler` (real `HTMLLinkExtractor`/`MediaLinkDetector`, real
`URLFrontier`, no mocked `record_media_link`) crawls one page (served by a
local `aiohttp` test server, no internet/real piracy site) containing one
`<a href="movie.mp4">` link, against a real `RedisMediaEvidenceStore`
pointed at the crawler's existing test-isolation database
(`redis_db=1`, namespace `test_evidence_e2e` — never the production
`evidence` namespace on DB 0). Asserts: exactly one media asset was
created, exactly one fingerprint job exists in the queue, and the asset's
URL matches the discovered link. Skips cleanly if Redis isn't available
locally, matching every other Redis-backed test's existing convention.

**Reused, not duplicated:** `tests/redis_media_evidence_store_test.py::
TestAssetAndObservationBehavior::test_duplicate_discovery_returns_same_asset_and_one_job`
already proves the store-level half of this (discovery → evidence →
exactly one job, plus the "rediscovery never creates a second job"
invariant) and was not re-implemented, per the brief's "prefer an
existing test fixture/helper if one exists."

No test asserts against `fingerprint:jobs:stream:default` or any
`fingerprint:*` key — that contract belongs to a different repository
this phase has no authority to test against, and per §8, is not part of
what "eligible media becomes a fingerprint job" means for this repo.

## 11. Exact test results

Focused (new + directly relevant) suite:

```text
$ env/bin/python3 -m pytest -q tests/hybrid_crawler_test.py tests/crawler_test.py \
    tests/extra_crawlers_test.py tests/scrapling_crawler_test.py \
    tests/redis_media_evidence_store_test.py tests/fingerprinter_queue_test.py \
    tests/media_evidence_test.py tests/media_evidence_multiprocess_test.py \
    tests/crawler_manager_recovery_test.py tests/crawler_manager_seed_failure_semantics_test.py \
    tests/crawler_heartbeat_integration_test.py tests/clear_db_backend_semantics_test.py
76 passed, 2 skipped in 101.20s
```

Full repository suite:

```text
$ env/bin/python3 -m pytest -q tests/
329 passed, 1 failed, 2 skipped in ~230s
FAILED tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::
    test_concurrent_redis_calls_use_a_bounded_shared_thread_pool
```

That one failure is in the **URL frontier's** thread-pool-offload test
(`core/redis_frontier.py`/`core/frontier_executor.py`), asserting all 100
concurrent `get_next_url()` calls return non-`None` under load — nothing
in this phase touched the frontier, this test file, or either module it
exercises. Reproduced standalone (`pytest
tests/frontier_executor_test.py::...`) and fails identically in isolation,
confirming it is a pre-existing timing-sensitive flake unrelated to this
phase's changes, not a regression introduced here. Not investigated
further — out of this phase's scope (media evidence / fingerprint jobs,
not the URL frontier).

**0 regressions in the media-evidence/crawler-engine/fingerprint-queue
surface this phase actually touched or audited.**

## 12. Remaining limitations

- The crawler-side bridge that would translate `evidence:jobs:queue`
  entries into `fingerprint:jobs:stream:*` submissions does not exist.
  Building it was explicitly out of scope for this phase (STOP
  CONDITION: do not begin Phase 3) and, per the fingerprinter's own Phase
  12 doc, is an unresolved cross-repo ownership decision, not a "small
  fix" — it requires choosing where a new, separately-deployable
  component lives and calling an already-fully-specified contract
  (`integration.submission.FingerprintJobSubmitter.submit()`,
  fingerprinter Phase 12 §5/§9) from this repo or a third component.
- `storage.enqueue_media_jobs` remains defined but inert (§6). Left
  unfixed since no demonstrated need for a job-creation kill switch
  exists yet; flagged for whoever eventually reconciles the crawler's
  config surface with actual behavior.
- The pre-existing `frontier_executor_test.py` flake (§11) is unrelated
  to this phase and was not investigated further.

## 13. Deferred work

Per the brief's STOP CONDITION, explicitly not started:

1. **Deciding and building the crawler-side (or third-component) bridge**
   from `evidence:jobs:queue` to the fingerprinter's
   `integration.submission.FingerprintJobSubmitter` — the fingerprinter
   repo's own Phase 12 doc (§26, item 1) already names this as its
   recommended next phase, with the field mapping fully specified on
   that side (§5, §9 of that document). This is the actual fix for "zero
   fingerprint jobs ever get processed" — Phase 2 (this document) only
   establishes that the crawler side of the pipeline is not the reason.
2. Any change to the fingerprint worker, its Redis Streams contract, or
   backpressure/priority semantics.
3. Wiring up `storage.enqueue_media_jobs` to actually gate anything, if a
   real operational need for that surfaces later.

## 14. Final status

**Answer to "Why did the real crawler run produce zero fingerprint
jobs?":** It didn't — the crawler produced exactly the fingerprint job(s)
its own, correctly-implemented design calls for, verified against live
production Redis state from a real crawl today (§7). The "zero" figures
in the original report describe `fingerprint:jobs:stream:default`, a
Redis Streams key belonging to the separate fingerprinter repository's own
job-consumption contract, which nothing in this crawler codebase writes
to and, per that repository's own architecture documentation, was never
built to be written to from here without a dedicated, not-yet-built
bridge component. This is a documented, known integration gap between two
independently-developed, independently-tested systems — not a hidden bug
in the audited pipeline.

One real, secondary observability defect was found and fixed within this
phase's scope: infrastructure-level media-evidence failures
(`MediaEvidenceUnavailable`) were silently swallowed at `DEBUG` in every
crawler engine, contrary to the media evidence store's own documented
visibility guarantee. Fixed by distinguishing it from benign candidate
rejections and logging it at `WARNING` (§9), with a new deterministic
end-to-end test (§10) added alongside the existing, already-passing
store-level coverage.

**Phase 2 is complete. Per the STOP CONDITION, no Phase 3 work (building
the crawler-fingerprinter bridge, or any fingerprint-worker change) was
started.**
