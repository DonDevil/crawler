# Media Evidence — Distributed Redis Architecture (Design Only)

Status: **architecture decision, no implementation**. Nothing in this document has been
built. It defines the data model, Redis keyspace, distributed claim/lease semantics,
durability boundary, and migration path for turning the current single-machine SQLite
media-evidence store into a fleet-wide distributed subsystem, following the same pattern
already proven for the URL frontier (`docs/architecture/frontier-adr.md`,
`docs/architecture/redis-sqlite-boundary-decision.md`).

Everywhere below, **CURRENT** describes what exists in the repository today (verified by
reading the source, not inferred). **PROPOSED** describes the new architecture. The two are
never blended without an explicit label.

---

## 0. Executive summary of the current gap

The crawler already runs a distributed, multi-machine, Redis-backed **frontier**
(`core/redis_frontier.py`) with proven claim/lease/heartbeat/recovery semantics. Media
evidence does not share any of that infrastructure:

- `storage/media_evidence_database.py` (`MediaEvidenceDatabase`) is **pure local SQLite**,
  instantiated identically regardless of `frontier.type` (`core/crawler_manager.py:70-74`
  — construction depends only on `config.crawler.storage.enable_media_evidence`, never on
  the frontier backend).
- There is **no multi-machine dedup**: if crawler A and crawler B on two different
  machines discover the same media URL, they write to two different local
  `storage/media_evidence.db` files. Nothing merges them today.
- There is **no fingerprinter fleet**: no DINOv2/pHash/audio code exists anywhere in this
  repository (`grep -rli fingerprint` outside `env/` returns only
  `storage/media_evidence_database.py`, `tests/fingerprinter_queue_test.py`, `main.py`).
  `main.py --claim-sample-job` / `--mark-match` are a CLI stub for a worker that does not
  exist yet. `README.md`'s "Image/Video/Audio fingerprinting" bullets are aspirational.
- `claim_next_sample_job()` has **no lease, no heartbeat, no ownership token, and is not
  concurrency-safe**: it does a plain `SELECT` then `UPDATE`, so two processes calling it
  against the same SQLite file can both read the same "first pending" row before either
  writes `claimed`. `complete_sample_job()` and `update_sample_job_status()` take no
  worker/token argument at all — **any caller can complete any job**, including a caller
  that never claimed it. This is strictly weaker than the frontier's pre-hardening state.

This document's job is to design the fix: a `MediaEvidenceStore` interface with a
`RedisMediaEvidenceStore` production implementation that reuses the frontier's proven
distributed-systems primitives (atomic Lua claim, CAS ownership token, lease + heartbeat,
periodic reclaim sweep) while **not** copying the parts of the frontier that don't apply
(per-domain fairness/scan-window bounding — fingerprint jobs aren't rate-limited per source
domain the way crawl requests are).

---

## Architecture Boundaries

*(Added in the revision pass — this is the single most load-bearing section in the
document; every other section must agree with it. Where an earlier section conflicted with
this one, the earlier section was corrected, not this one.)*

```
Production:
    RedisMediaEvidenceStore — the sole authoritative production backend for media evidence.

Development / testing:
    SQLiteMediaEvidenceStore — independent, standalone. Not a staging step toward Redis,
    not a fallback path, not exercised in production.

Synchronization between the two backends:
    NONE. No mirroring, no fallback, no export, no import, in either direction, at any
    point in the lifecycle. A process talks to exactly one backend for its entire run.

Production SQL dependency:
    NONE. No synchronous or asynchronous SQL write occurs anywhere on the crawler hot path
    or the fingerprint-completion path in production. Redis persistence (AOF/RDB — §14) is
    the entire production durability story for this phase.

Fingerprinting algorithms (DINOv2, pHash, audio, temporal verification, FFmpeg):
    Live entirely outside Media Evidence, in the (not-yet-built) `fingerprinter/` package
    (§23). Media Evidence stores what they produce; it does not implement, tune, or version
    them.

Large media / embeddings:
    Never stored in a Redis evidence hash or any other Media Evidence structure. If a
    future requirement needs persisted embeddings, that is a distinct, future, out-of-scope
    decision (§14) — not something this design pre-selects a store for.
```

This corrects the previous revision of this document, which proposed exporting
confirmed-match evidence into the existing SQLite file as a "one-way archival record." That
proposal is withdrawn (§14) — it put SQL back on the production write path for a
high-value event, which is exactly the dependency this section forbids, regardless of how
narrowly the write was scoped or how it was framed.

---

## 1. Current data flow (verified, not inferred)

```
crawler engine (async/http/tor/playwright/selenium/scrapling/hybrid)
      │  MediaLinkDetector.extract_media_links() / direct-response content-type sniff
      │  StreamingManifestParser.parse_manifest() for HLS/DASH
      ▼
MediaEvidenceDatabase.record_media_link(url, source_page, referrer_url,
                                         discovered_by, discovery_method,
                                         media_type, mime_type, content_length, priority)
      │  three separate, separately-committed SQLite writes (see §17 bug note):
      │   1. UPSERT media_assets (dedup key = url, after URLUtils.clean_media_url)
      │   2. INSERT media_observations (asset_id FK)
      │   3. UPSERT sample_jobs (asset_id UNIQUE FK, status='pending' on first insert)
      ▼
[STOPS HERE TODAY] — no code claims/consumes sample_jobs automatically.
      │
      │  manual/future: `python main.py --claim-sample-job --worker-name X`
      ▼
MediaEvidenceDatabase.claim_next_sample_job(worker_name)
      │  SELECT first pending job (priority ASC, updated_at ASC) → UPDATE status='claimed'
      │  NOT ATOMIC. NOT LEASED. NOT TOKEN-GUARDED.
      ▼
[NO CODE EXISTS: media download, fingerprinting (DINOv2/pHash/audio), result storage]
      │
      │  manual/future: `python main.py --mark-match <asset_id> --match-title ... --match-confidence ...`
      ▼
MediaEvidenceDatabase.mark_asset_matched(asset_id, matched_title, confidence, domain_database, score_increment)
      │  → complete_sample_job(asset_id, fingerprint_status='matched', ...)
      │  → domain_database.add_or_update(source_domain, score=current+increment)   [DIRECT COUPLING]
      ▼
crawler feedback: source_domain's DomainDatabase score increases (storage/domain_database.py)
```

`record_manifest_variants(asset_id, variants)` is called immediately after
`record_media_link()` whenever the classified `media_type == "stream-manifest"`, storing
each `{url, bandwidth, resolution, codecs}` row keyed by `(asset_id, variant_url)`. Variants
are descriptive metadata only — they never get their own `sample_jobs` row; the manifest
URL itself is what gets fingerprinted.

`crawler_manager.py` wires exactly one `MediaEvidenceDatabase` instance per crawler process,
from `config.crawler.storage.media_sqlite_path` — this is completely independent of
`config.crawler.frontier.type`. Running the frontier in `redis` mode today gives you a
distributed URL queue and a **non-distributed, per-machine** media evidence store.

### Bugs found during investigation (documented, not fixed — out of scope per instructions)

1. **`claim_next_sample_job` race.** Read-then-write, no transaction spanning both
   statements, no `claimed_by`/token check on completion. Fine today because nothing calls
   it concurrently; unsafe the moment two fingerprinter processes exist.
2. **`complete_sample_job` / `update_sample_job_status` have no ownership check.** Any
   caller can mutate any asset's job state by `asset_id` alone.
3. **`record_media_link` is not atomic across its three writes.** `BatchedDatabaseWriter`
   commits on every `execute()` call (`storage/async_database_writer.py:39-48` — despite the
   class being named "batched," each call flushes immediately for cross-connection read
   visibility). A crash between steps 1 and 3 can leave an asset with no job row.
4. **`sample_jobs.retry_count` is declared but never read or incremented anywhere** in
   `MediaEvidenceDatabase`. Dead column.
5. **No lease, no expiry, no recovery for a stuck/crashed claim.** A job claimed by a
   process that then dies stays `claimed` forever.
6. **`fingerprint_status` is a free-form string**, not a validated enum — passed straight
   through from caller to `UPDATE ... SET status = ?`.

### 1a. Source-verified `record_media_link()` semantics (re-audit for the Lua design)

Re-read `storage/media_evidence_database.py` line-by-line specifically to answer the nine
questions the revision requested, because `record_media_link()` is about to become one
atomic Lua script and an incorrect interpretation of today's semantics would get baked in
permanently. Two additional bugs were found on this pass that the first investigation
missed; both are noted below and folded into the numbered bug list above's spirit.

1. **What constitutes a new asset?** A row insert into `media_assets` triggered by the
   `INSERT ... ON CONFLICT(url) DO UPDATE` finding no existing row for
   `URLUtils.clean_media_url(url)`. Identity is the cleaned URL string, exactly as §3
   already models via `discovery_id`.
2. **What constitutes a new observation?** Every single call to `record_media_link()`
   unconditionally inserts one `media_observations` row — there is **no deduplication of
   observations at all**, by content or otherwise (confirmed: no `UNIQUE` constraint, no
   `ON CONFLICT` clause on that `INSERT`). Two calls with byte-identical arguments produce
   two rows differing only by `id` and `observed_at`. This is best read as **intentional
   event-log semantics** (each call is a real discovery *event*, even if the page/URL pair
   repeats — a retry or a later re-crawl is legitimately a new event), not an oversight —
   there would be nothing to key a dedup constraint on that wouldn't also suppress
   legitimate re-observation.
3. **When is a fingerprint job created?** Only on the `INSERT` branch of the
   `sample_jobs` upsert — i.e. only the very first time `record_media_link()` is called for
   a given asset. The `ON CONFLICT(asset_id)` branch never creates a row.
4. **What happens when the same (not-yet-terminal) asset is rediscovered?**
   `media_assets`: mutable fields update (`last_seen`, `last_source_page`/`last_referrer_url`
   via `COALESCE`, `last_discovered_by`/`last_discovery_method` via straight overwrite),
   `status` resets to `'queued_for_sampling'`. `sample_jobs`: `priority` becomes
   `MIN(existing, new)` (rediscovery can only make a job *more* urgent, never less — a
   clearly intentional ratchet), `updated_at` bumps, and — see finding (a) below — `status`
   does not actually change.
5. **What happens when a completed (`matched`) asset is rediscovered?** `media_assets.status`
   is protected by the `CASE WHEN media_assets.status IN ('matched', 'hashed') THEN
   media_assets.status ELSE 'queued_for_sampling' END` clause (`storage/media_evidence_database.py:136`)
   — it does not get reset. A **new observation is still appended** — rediscovery of an
   already-matched asset continues to accumulate evidence of ongoing distribution, which
   reads as intentional and worth preserving. `match_confidence`/`matched_title` are
   untouched by `record_media_link()` (they're only ever written by `complete_sample_job`).
6. **What happens when a permanently-failed asset is rediscovered?** **No such state exists
   in the current implementation at all** — there is no `permanent_failure`/`failed` value
   anywhere in `media_evidence_database.py`, no retry-exhaustion logic, nothing. This
   question has no current answer to preserve; §5a below records it as a **new** decision,
   not a preserved one.
7. **Can a new observation ever legitimately create a new job?** No — confirmed by (3):
   job creation is strictly gated to first-ever discovery of the asset row, with no
   exception. This holds for every status the asset can be in, including a hypothetical
   future `permanent_failure` (§5a) unless a later explicit design decision says otherwise.
8. **What exact behavior must Redis preserve?** (a) asset dedup by normalized URL: yes; (b)
   unconditional, undeduplicated observation append per call: yes (this is why §4's cap is
   a *new* Redis-specific mitigation, not a preserved SQLite behavior — SQLite retains
   unbounded history today); (c) job created exactly once, at first discovery, never again:
   yes; (d) rediscovery may only make priority more urgent (`MIN`), never less: yes; (e)
   rediscovery must never disturb a job once one exists — see finding (a) immediately below
   for why the *mechanism*, not just the *outcome*, needs correcting in the Lua version.
9. **Which current behavior is accidental SQLite artifact rather than intentional domain
   semantics?** Two additional, newly-confirmed findings, beyond the six already listed
   above:

   **(a) The `sample_jobs` status-protection `CASE` is dead code — a tautology.**
   `storage/media_evidence_database.py:184-188`:
   ```sql
   status = CASE
       WHEN sample_jobs.status IN ('sampled', 'hashed', 'matched') THEN sample_jobs.status
       ELSE sample_jobs.status
   END
   ```
   Both branches evaluate to `sample_jobs.status` — the condition is checked but never
   changes the result. This reads as an unfinished edit (the author likely meant to protect
   only the terminal-state branch and reset otherwise, mirroring the `media_assets` clause
   directly above it, but never wrote a differing `ELSE`). **The accidental *effect*,
   however, happens to be the domain-correct one**: job `status` is never modified by
   rediscovery, under any condition, which is exactly what (8)(e) requires. The Redis
   design should **preserve the effect deliberately** (rediscovery never touches an
   existing job's status, full stop — no status list to enumerate or get wrong) rather than
   reproducing the broken mechanism (a conditional that doesn't actually condition).

   **(b) `media_assets.status` and `sample_jobs.status` can diverge, because the two
   `CASE` clauses protect different, inconsistent status sets.** The asset clause protects
   only `('matched', 'hashed')` (`:136`); the job clause's condition-that-doesn't-matter
   lists `('sampled', 'hashed', 'matched')` but is irrelevant per (a) since it never resets
   anyway. Concretely: an asset with an **actively claimed** job (`sample_jobs.status =
   'claimed'`) that gets rediscovered has its `media_assets.status` reset to
   `'queued_for_sampling'` — while `sample_jobs.status` correctly stays `'claimed'` (by
   accident, per (a)). The two tables now disagree about whether the asset is being worked
   on. This is a genuine, confirmed bug, not a design choice worth preserving. **Redis
   correction (recommended, new): do not give the asset its own independently-writable
   `status` field at all.** Derive the asset's displayed status from the Job's status plus
   the Result's `aggregate_decision` (read-through, computed at read time or updated
   transactionally in the *same* Lua script that updates the Job, never as a second
   independently-racing write) so this class of divergence is structurally impossible
   rather than merely avoided by convention. This is a deliberate, documented deviation
   from SQLite's two-independent-fields shape — see §2 and §12.

   **(c) `'sampled'` and `'hashed'` are unreachable placeholder states — never produced by
   any code path.** Repo-wide re-grep for the literal strings `'sampled'` and `'hashed'`
   (outside `env/`) finds them **only** inside the two `CASE` clauses discussed above —
   never as a value passed to `complete_sample_job`, `update_sample_job_status`, or
   asserted in any test. The only `fingerprint_status` value ever actually exercised,
   anywhere in the codebase, is `"matched"` (via `mark_asset_matched`, and directly in
   `tests/fingerprinter_queue_test.py:28`). These two names most likely represent an
   intended (but never wired up) multi-stage pipeline — "sampled" (media downloaded) →
   "hashed" (fingerprint computed) → "matched"/"rejected" (decision made) — not random
   garbage, but they are **not implemented behavior to preserve**. §5 already collapsed the
   proposed Redis lifecycle to `queued → claimed → completed(confirmed|rejected|uncertain) |
   retry_scheduled | permanent_failure` without them; this re-audit confirms that
   collapse was correct, not merely simpler. If the fingerprinter pipeline later wants
   sub-stage visibility, that belongs in job metadata/heartbeat payload (e.g. "current
   stage: frame-extraction"), not as new top-level Job states — consistent with "don't
   preserve obsolete states merely for compatibility."

   **(d) `update_sample_job_status()` has zero callers anywhere in the repository**,
   including tests — confirmed by a repo-wide grep for its name, which returns only its own
   definition. It is dead code, not an exercised part of the current contract. §17's
   proposed `fail_fingerprint_job()` replaces its intended role (generic status/error
   setter) with an explicit, token-guarded, retry-aware method — this is a redesign of
   unused code, not a behavior migration.

---

## 2. Domain model — what's actually necessary

Investigated against the "only introduce entities the system or distributed requirements
justify" rule. Four entities survive; two candidates were rejected.

| Entity | Keep? | Why |
|---|---|---|
| **Media Asset** | Yes | The one thing multiple crawlers must agree on the identity of. Everything else hangs off it. |
| **Media Observation** | Yes, but capped | Evidence of *who/where* discovered it — needed for investigation and per-domain feedback; unbounded retention is not needed (§4, §15). |
| **Manifest Variant** | Yes, as bounded metadata on the asset, not an independent entity | Current behavior: descriptive only, never independently fingerprinted. No lifecycle of its own. |
| **Fingerprint Job** | Yes | Renamed/hardened form of `sample_jobs`. This is the thing that needs the distributed claim/lease machinery. |
| **Fingerprint Attempt** (as a distinct entity/log) | **Rejected** | Current system has no attempt history, only a counter + `last_error` string. A full attempt log isn't justified by any current behavior or requirement — a `retry_count` + `last_error`/`error_class` field on the Job is sufficient, same shape as the frontier's `attempts:{url}` counter. |
| **Fingerprint Result** | Yes, separate from Job | Job = transient operational/queue state (can be discarded after completion). Result = durable evidence output (scores, decision, confidence) that must outlive the queue entry. Conflating them (as SQLite does today, writing match fields directly onto `media_assets`) is what makes §14's durability question hard to answer cleanly — splitting them makes the durability boundary explicit. |
| **Match/Confirmation** | **Rejected as a separate entity** | A confirmed match is a *value* of `FingerprintResult.aggregate_decision`, not a new entity — it needs an *event* (§19 feedback channel) more than it needs its own storage record. |

### Per-entity detail

**Media Asset**
- Identity: see §3.
- Lifecycle: `discovered → queued_for_fingerprint → claimed → (completed: matched | rejected | uncertain)`, with `permanent_failure` as an absorbing state for unfingerprintable content. **Correction from the §1a re-audit**: unlike current SQLite (where `media_assets.status` is an independently-writable field that can desync from `sample_jobs.status` — a confirmed bug, §1a finding (b)), the asset's displayed status must be a **read-through of the Job's status and the Result's `aggregate_decision`**, never a second field updated by a separate, independently-racing write. Rediscovery (`record_media_link`) must never write to this field at all once a Job exists for the asset.
- Immutable at creation: canonical URL, `first_seen`, `discovery_id`.
- Mutable: `last_seen`, `observation_count`; `status`/`match_confidence`/`matched_title` are written only by Job/Result transitions (§5, §9), never by rediscovery. `content_id` is an optional, later-populated cross-reference — see §3's revised treatment; it is not assumed to come from any one specific algorithm.
- Cardinality: one per distinct canonical URL, fleet-wide. Target scale in §15.

**Media Observation**
- Represents one discovery event: which crawler, which page, when, with what HTTP metadata.
- Necessary fields (justified by current callers — see §4 for the full argument):
  `source_page`, `referrer_url`, `discovered_by` (node id), `discovery_method`, `mime_type`,
  `content_length`, `observed_at`.
- Cardinality: many-to-one with Asset, **unbounded in principle, bounded in storage** (§4,
  §15, §16 — this is the field most exposed to abuse/flood scenarios).

**Manifest Variant**
- `{variant_url, bandwidth, resolution, codecs}`, keyed under the manifest asset.
- Bounded per-asset list (current SQLite already effectively caps this at whatever a real
  HLS/DASH manifest contains; Redis needs an explicit cap against a malicious manifest with
  thousands of fabricated variants — §16).

**Fingerprint Job**
- One active job per asset (current `sample_jobs.asset_id UNIQUE` already encodes this
  invariant — preserved).
- Owns: `status`, `priority`, `retry_count`, `error_class`, `last_error`, claim
  ownership/lease bookkeeping (§5–§7).

**Fingerprint Result**
- Owns: per-algorithm scores (DINOv2 similarity, pHash, audio), `aggregate_decision`,
  `confidence`, `algorithm_versions`, `worker_id`, `processed_at`. Never large binary data
  (§9).

---

## 3. Media asset identity — discovery identity vs. content identity

This is the central correctness decision for fleet-wide dedup, and the prompt is right to
flag it: **the crawler discovers a URL, it does not download the media**, so a content hash
cannot be the discovery-time identity — nothing has been hashed yet.

### Discovery identity (established at discovery time, zero coordination required)

```
discovery_id = sha256(URLUtils.clean_media_url(raw_url))
```

`URLUtils.clean_media_url` → `URLUtils.normalize_url` (already exists, `utils/url_utils.py:540-570`)
lowercases scheme/host, strips the fragment, and strips known tracking parameters
(`TRACKING_PARAMETERS`) — but **does not sort or otherwise canonicalize remaining query
parameters**. This matters concretely: HLS/DASH CDN URLs routinely carry expiring
auth-signature query parameters (`?token=...&expires=...`). Two crawlers discovering "the
same" `master.m3u8` seconds apart, or the same crawler re-fetching a page whose CDN reissues
tokens, will normalize to two different `discovery_id`s today. **This is a known, accepted
limitation of using discovery identity alone** — flagged here, not fixed here (fixing it
would mean either stripping a signed-URL-parameter allowlist, which is site-specific and
fragile, or deferring entirely to content identity, which requires a download that hasn't
happened yet). Recommendation: proceed with discovery identity as designed below; treat
signed-URL fragmentation as a known source of duplicate assets that content identity (once
populated) will retroactively link, not merge.

Using a hash of the normalized URL as the literal Redis key (rather than an
auto-incrementing integer id, which is what SQLite uses today) is a deliberate improvement:
**any of the N crawler machines can compute the identical key locally, with no round trip
and no coordination**, before ever talking to Redis. This is a genuine advantage over both
(a) SQLite's `AUTOINCREMENT`, which is inherently single-writer, and (b) the frontier's own
`urls:known` SET, which needs an existence check because frontier IDs are the URLs
themselves (fine for a SET) — here we go one step further and make the *key* deterministic,
which lets asset creation be a single idempotent Lua script with no prior lookup at all
(§11, §12).

### Content identity — kept deliberately abstract, not fixed to any one algorithm

The first revision of this document defined `content_id` as "a perceptual/content hash
produced by the fingerprint pipeline," singular. That's too specific for a design phase
that explicitly must not design fingerprinting algorithms (§9 boundary, reinforced by the
Architecture Boundaries section) — it silently assumed a single canonical hash exists,
before the pipeline that would produce it has been built or benchmarked. Corrected here by
naming three genuinely distinct concepts that must not be conflated:

1. **URL / discovery identity** (`discovery_id`, above) — deterministic, computed instantly
   at discovery time from the URL alone, requires no download. This is fixed and settled by
   this design.
2. **Cryptographic content hash** (e.g. a hash of the downloaded media bytes) — exact-match
   only; two byte-identical files hash identically, a single re-encoded frame does not.
   Cheap to compute once bytes exist, but only useful for detecting literal duplicates, not
   re-encodes/transcodes/mirrors, which is most of what this pipeline needs to catch.
3. **Perceptual similarity fingerprint(s)** — DINOv2 embedding similarity, pHash, audio
   fingerprints, temporal verification, etc. (§9). Approximate-match, algorithm-specific,
   versioned, and — critically — there is no requirement anywhere in this investigation
   that exactly one such fingerprint is "the" content identity. The eventual pipeline may
   use several in combination (§9 already models per-algorithm scalar scores on
   `FingerprintResult` for exactly this reason).

**Revised model:** the Media Asset carries an *optional* `content_id` field whose concrete
meaning (which of the above, or a composite of several) is intentionally left as an
abstraction boundary for the fingerprinter to define once it exists — Media Evidence's
contract is only that *if* the fingerprinter supplies one or more content-identification
values, Media Evidence will index and link on them; it makes no assumption about how they
were computed. Concretely: `{ns}:content:{content_id}:assets` (§12) is generic — one or more
such reverse-index keys may exist per asset (e.g. a cryptographic-hash index and a
perceptual-fingerprint index, populated independently, at different pipeline stages), not
necessarily one. Used to **link**, never to **merge**, assets: if two different
`discovery_id`s (e.g. a mirror CDN, or a signed-URL variant of the same segment) turn out to
share a content identity of either kind, record the relationship via the reverse index.
Merging discovery-identity records retroactively would require rewriting job/observation/
result history across a live distributed system for no operational benefit — the linkage is
enough for investigation and for avoiding redundant *fingerprinting work* (§11) without
touching already-written evidence.

The exact shape of `content_id` (single hash vs. multiple typed fingerprint references) is
left as an open question for whoever designs the fingerprinter (§ Appendix) — Media
Evidence's job is to have a slot for it and a generic linking mechanism, not to decide what
goes in the slot.

---

## 4. Media observations — what's necessary, what isn't

Necessary (each ties to a concrete current caller or the stated deduplication/evidence/
feedback requirements):

- `source_page`, `referrer_url` — needed for investigation (which page hosted the pirated
  content) and are exactly what crawler engines already pass at every one of the 8 call
  sites found in `crawler/*.py`.
- `discovered_by`, `discovery_method` — needed to distinguish "async direct-response sniff"
  from "playwright network-response" from "anchor tag" etc.; used today, asserted on in
  `tests/media_evidence_test.py:49`.
- `mime_type`, `content_length` — needed to corroborate `media_type` classification and as
  evidence.
- `observed_at` — needed for ordering/dedup-window logic and evidence timestamping.

Explicitly **not** retained per-observation, because nothing in the current system or the
distributed requirements needs it: full request/response headers, cookies, IP addresses,
crawl-session ids beyond `discovered_by`. Retaining "everything available" is exactly the
anti-pattern the brief warns against.

**Cardinality control (this is the important distributed-systems decision for this
entity):** an asset discovered from 100 source pages, or flooded with thousands of
duplicate observations from one malicious source (scenarios in §20), must not create
unbounded Redis growth. Proposed model: keep an exact `observation_count` (cheap `HINCRBY`,
never lossy — needed as real evidence of prevalence) plus a **capped ring buffer** of the
most recent detailed observations, capped via `LPUSH` + `LTRIM`. The count is durable
evidence of scale; the buffer gives investigators recent provenance without paying for
unbounded history.

The cap itself is a **configurable value, `max_observations_per_asset`**, not a fixed
architectural constant — an initial default of 20 is reasonable to start implementation
with, but the right number depends on real observation volume/investigation needs that
don't exist yet, and should be revisited once the benchmark in §22 produces real numbers.
Nothing in this design depends on the cap being exactly any particular value; every
reference to "20" elsewhere in this document should be read as that same configurable
default, not a separate decision.

---

## 5. Fingerprint job — lifecycle state machine

Derived from current behavior (`pending → claimed → {sampled|hashed|matched}` observed in
code, with no retry/failure branch implemented) plus the distributed requirements (§6–§8):

```
        queued
          │  claim(token, worker_id, lease_ttl)
          ▼
        claimed  ───heartbeat/renew_lease──┐
          │                                 │
          │ (fingerprinter reports          │
          │  progress optionally)           │
          ▼                                 │
        processing ◄───────────────────────┘
          │
          ├── complete(token, result=confirmed|rejected|uncertain) ──► completed
          │
          ├── fail(token, error_class, retryable=true) ──► retry_scheduled
          │         │ (backoff elapses)
          │         ▼
          │       queued            (retry_count += 1)
          │
          ├── fail(token, error_class, retryable=false) ──► permanent_failure
          │
          └── lease expires, no renew, no complete ──► [recovery sweep] ──►
                    retry_scheduled (if retry_count < max_retries)
                    permanent_failure (otherwise)
```

`claimed` and `processing` are collapsed into one operational state in the proposed Redis
model (a single `claimed` state with a lease, renewed by heartbeat) rather than two —
current code never distinguishes "claimed but not yet started" from "actively processing,"
and nothing in the requirements needs that distinction; the heartbeat itself *is* the
"still alive and working" signal, exactly as the frontier already establishes for URL
fetches.

Whether a failure is retryable is **decided by the fingerprinter, not the store** (§8, §17,
§19): the store receives a structured `(error_class, retryable: bool)` from the caller and
mechanically applies the same generic backoff/permanent-failure machinery regardless of
which specific error produced it. The store should not need to know what "corrupt media"
means; it only needs to know whether to retry.

### 5a. Source-verified job lifecycle semantics (re-audit: `claim_next_sample_job`,
`complete_sample_job`, `update_sample_job_status`, `mark_asset_matched`)

- **`claim_next_sample_job(worker_name)`** — `SELECT` the first `pending` job (`priority
  ASC, updated_at ASC`), then `UPDATE sample_jobs SET status='claimed', claimed_by=...`
  **and** `UPDATE media_assets SET status='claimed', last_seen=...` — both tables are kept
  in sync **at this call site**. (The divergence bug in §1a finding (b) only happens via
  the *rediscovery* path, `record_media_link`, never here.)
- **`complete_sample_job(asset_id, fingerprint_status, match_confidence, matched_title,
  last_error)`** — sets `sample_jobs.status = fingerprint_status` **and**
  `media_assets.status = fingerprint_status` together (also kept in sync at this call
  site). `fingerprint_status` is accepted as an arbitrary caller-supplied string with zero
  validation — confirmed no enum/allowlist anywhere in the method.
- **`update_sample_job_status(asset_id, status, last_error)`** — same dual-write shape as
  `complete_sample_job`, generic status setter. **Confirmed dead code**: zero callers
  anywhere in the repository outside its own definition (§1a finding (d)). Not a behavior
  to migrate — its intended role is superseded by the new, token-guarded
  `fail_fingerprint_job()` (§17).
- **`mark_asset_matched(asset_id, matched_title, confidence, domain_database,
  score_increment)`** — the only production-shaped entry point that's actually exercised
  end-to-end (via `main.py --mark-match` and `tests/fingerprinter_queue_test.py`). Always
  calls `complete_sample_job(..., fingerprint_status="matched", ...)` — hardcoded, never
  parameterized — then separately reads `source_domain` and calls
  `domain_database.add_or_update()` directly. This direct coupling is the exact thing §19
  replaces with the `confirmed_match` event.

**Mapping current states to the proposed Redis lifecycle** (§5), with intent classification
per §1a finding (c):

| Current SQLite value | Ever actually produced? | Redis treatment |
|---|---|---|
| `pending` | Yes (initial) | → `queued` |
| `claimed` | Yes (`claim_next_sample_job`) | → `claimed` (with lease, §7) |
| `sampled` | **No — never produced anywhere**, exists only inside a dead `CASE` clause | Dropped as a top-level state; a future sub-stage signal, if ever needed, belongs in job metadata, not a formal state (§1a(c)) |
| `hashed` | **No — never produced anywhere**, same as above | Same treatment |
| `matched` | Yes (`mark_asset_matched`, the only exercised terminal value) | → `completed` with `Result.aggregate_decision = confirmed` |
| *(no `rejected`/`uncertain` equivalent exists today)* | N/A — never implemented | New, required by §9's result model; not a preserved behavior |
| *(no failure/retry state exists today)* | N/A — never implemented | New: `retry_scheduled`, `permanent_failure` (§5, §8) — entirely new design, not migrated from anything |

**Open domain-semantic question this re-audit could not resolve from source alone** (flagged
here rather than silently decided): should rediscovery of a `permanent_failure` asset ever
reopen it? No current behavior exists to preserve either way (§1a, question 6) — by analogy
with the `matched`/`hashed` protection pattern (terminal states are not disturbed by mere
rediscovery), this document's working recommendation is **no, `permanent_failure` should
not be automatically reopened by rediscovery alone** (it would otherwise let a source that
generates many distinct URLs for the same unfingerprintable content — §16 — retrigger
fingerprint attempts indefinitely), leaving only an explicit administrative re-queue
operation as the way back to `queued`. This is called out as a recommendation, not a closed
decision — it deserves product sign-off before implementation, not just architectural
inference.

---

## 6. Distributed job claiming

Directly reuses the frontier's proven pattern (`core/redis_frontier.py`, extracted in full
during this investigation) with one deliberate simplification.

**What's reused, unmodified in spirit:**
- Atomic, single-round-trip Lua claim: pop the highest-priority eligible job, write a
  claim-ownership record keyed by a fresh `uuid4` token, add the job to an `inflight`
  ZSET scored by `now + lease_ttl`. Exactly the frontier's `get_next_url` shape.
- CAS-guarded mutation: every subsequent operation on a claimed job (`renew`, `complete`,
  `fail`) starts by checking the presented token against the stored claim token; mismatch
  or missing key → the operation is rejected as a no-op, never applied. This is precisely
  what closes bug #2 in §1 (today's `complete_sample_job` has *no* ownership check at all).
- Periodic reclaim sweep, not lazy-on-read: a background loop scans `inflight` for
  entries whose lease has passed, using the exact same `ZRANGEBYSCORE ... LIMIT
  0 batch_size` bounded-batch pattern the frontier's `reclaim_and_promote` uses, so
  recovery cost is independent of total job count. Safe to run redundantly from every
  process (the frontier already relies on this — every crawler machine runs its own
  recovery loop against the same Lua script with no leader election needed, because the
  Lua CAS makes concurrent sweeps idempotent).

**What's deliberately *not* copied — the frontier's per-domain scan-window bound
(`domain_scan_limit`, `domain_heads` ZSET) exists to solve a problem fingerprint jobs don't
have.** That machinery bounds `get_next_url`'s worst-case work when many *domains* are
simultaneously rate-gated and an unbounded scan across domains would be needed to find one
that's eligible. Fingerprint jobs are not naturally partitioned by source domain in a way
that creates the same fairness/starvation risk — there's no "wait N seconds before claiming
another job from this domain" rule. A single global priority `ZSET` with an O(log N)
`ZPOPMIN`-shaped claim is sufficient and simpler; inventing a domain-sharded claim path here
would be exactly the kind of blind copy the brief warns against. (§24 revisits this as a
scaling watchpoint, not a decision reversal.)

**Lua is required** for: claim (read-check-write must be atomic across the queue pop +
claim-record write + inflight insert), renew (token check + inflight score update must be
atomic), complete/fail (token check + state transition must be atomic). Nothing in this
subsystem needs a client-side multi-step sequence — matching the frontier's own rule that
every mutation is one script, one round trip.

---

## 7. Lease + heartbeat

Fingerprinting is minutes, not milliseconds — download, FFmpeg, DINOv2 inference, temporal
verification, pHash, audio analysis, in sequence. The frontier's `lease_ttl=90s` /
`heartbeat_interval=lease_ttl/3` defaults exist for single-HTTP-fetch timescales and are
wrong here by roughly two orders of magnitude.

**Configurable, not fixed architecture constants** (config, not hard-coded — mirrors
`FrontierConfig.lease_ttl`/`heartbeat_interval`). The architecture defines two named config
values and one invariant; it does not fix their numbers:

- `fingerprint_lease_ttl` — configurable. **Initial default for implementation to start
  with: 900s (15 min)**, generous enough to cover a worst-case video download + full
  pipeline given no real timing data yet exists. This number is explicitly provisional —
  actual pipeline timing (once the fingerprinter exists and is benchmarked, §22) should
  drive the real value, and it may need to differ by media type/size rather than being a
  single global constant.
- `fingerprint_heartbeat_interval` — configurable, defaulting to `lease_ttl / 3` by the
  same formula as `core/claim_heartbeat.py:default_heartbeat_interval` (initial default
  ~300s, following from the 900s initial default above).
- **The one invariant that is architecture, not a tunable default**: heartbeat must occur
  with sufficient margin before lease expiration, enforced the same way the frontier
  enforces it — `heartbeat_interval` is always clamped below `lease_ttl` regardless of
  configuration, so a live worker can never be starved of renewal attempts by
  misconfiguration. This clamp relationship is the actual design decision; 900s/300s are
  just where implementation should start.

Unlike a single fetch, a fingerprint job has discrete pipeline stages (download complete →
frames extracted → embeddings computed → comparison done). The **mechanism is identical**
to `run_with_heartbeat` (race the work against a renewal timer, treat a failed renew as
`ClaimLostError` and abandon in-flight work immediately), but the fingerprinter is free to
call `renew_job_lease` opportunistically between stages rather than needing a tight
background timer loop, since stage boundaries naturally occur well inside the
15-minute-default lease window.

A stale worker's completion is rejected exactly as in §6: `complete_fingerprint_job`
performs the same token CAS check as the frontier's `_complete_claim_script`. Worker A,
reclaimed by the recovery sweep after its lease expired, has its token invalidated the
moment worker B successfully claims the job (the claim record is overwritten); A's later
`complete()` call fails the CAS check and is a silent, logged no-op — no state corruption
(this is scenario 4 in §20, walked through explicitly there).

---

## 8. Retry semantics

The store applies one generic state machine (§5); **retryability classification is the
fingerprinter's responsibility**, passed in explicitly rather than inferred by the store
from a free-text error string (fixing bug #6 from §1's audit). Analysis per failure class,
for the fingerprinter's own logic to implement later (no fingerprinting code exists yet —
this is guidance for that future implementation, not a spec being built now):

| Failure | Retryable? | Rationale |
|---|---|---|
| Transient network failure (timeout, connection reset) | Yes | Classic retry-with-backoff case. |
| Temporary HTTP failure (429, 5xx) | Yes | Same shape as the frontier's own retry-on-5xx logic (`crawler/async_crawler.py:92`). |
| Corrupt media (fails to decode) | No | Deterministic — retrying won't fix bad bytes. |
| Unsupported media format | No | Deterministic. |
| FFmpeg failure | **Depends** — the fingerprinter must sub-classify (e.g. a nonzero exit from a codec probe vs. an OOM/crash) | Cannot be decided generically; store just receives whatever `retryable` flag the fingerprinter derives. |
| Model/inference failure (GPU OOM, model load error) | Yes, with a smaller max-retry budget | Usually transient infra flakiness, but expensive to retry blindly — recommend a distinct `max_retries_model` config lower than the general default. |
| Worker crash (no error reported at all) | Always recoverable | Not a reported failure — detected purely by lease expiry in the recovery sweep, with no `last_error` set; distinguishable from a reported failure by an empty error field. |

Mechanics (shared, generic, store-owned): `retry_count` increments on every
`retry_scheduled` transition; exponential backoff using the same shape as the frontier's
`base_backoff * 2^retry_count` capped at `max_backoff`; `max_retries` config (recommend a
smaller default than the frontier's `3`, e.g. `2`, given fingerprinting cost); exceeding
`max_retries` → `permanent_failure`, a terminal absorbing state. `error_class` and
`last_error` (bounded length — see §16) are stored on the Job for diagnostics regardless of
retry outcome.

---

## 9. Fingerprint result model

No existing fingerprinting implementation exists to preserve behavior from
(§0) — this section is genuinely new design, scoped tightly to "what does the distributed
evidence layer need to represent," not "how do the algorithms work" (out of scope,
untouched).

**Operational state** (Job, §5): status, priority, retry_count, error_class, claim/lease.
Lives in Redis only, discardable after completion + a short retention window.

**Evidence/result** (new `FingerprintResult`, one per completed asset — or, if
re-fingerprinting is ever supported, latest-wins with history out of scope for now):
`asset_id`, `algorithm` results as separate scalar fields (`dinov2_similarity`,
`phash_score`, `audio_score`, `temporal_verified: bool`), `aggregate_decision`
(`confirmed | rejected | uncertain`), `confidence`, `algorithm_versions` (small JSON blob —
model/version strings, not weights), `worker_id`, `processed_at`. This is durable evidence,
subject to the durability decision in §14, not just operational bookkeeping.

**Explicitly excluded from Redis** — large binary data: DINOv2 embedding vectors, extracted
frames, downloaded media bytes, audio waveforms. **If** a future requirement needs
persisted embeddings for cross-asset nearest-neighbor search, that belongs in a
purpose-built vector store or object storage, referenced from the `FingerprintResult` by a
URI/pointer field — never embedded inline in a Redis hash. This is a hard boundary, not a
soft preference: Redis memory cost scales with what's stored, and embeddings are large
relative to everything else this subsystem stores (§15).

---

## 10. Manifests and variants — minimum representation

No redesign of manifest parsing (`parsers/streaming_manifest_parser.py` stays untouched).
The relationship preserved from current behavior:

```
manifest asset (media_type="stream-manifest")
   └── variants: [{variant_url, bandwidth, resolution, codecs}, ...]
```

Variants remain descriptive metadata attached to the manifest asset — never independently
fingerprinted, never given their own Job, matching current SQLite behavior exactly (nothing
in `record_manifest_variants` today creates a `sample_jobs` row). Proposed storage: a
bounded list/hash under the asset's key (§12), capped at a fixed count (proposed 20,
ordered by bandwidth descending, matching the existing `ORDER BY bandwidth ASC` read query's
implicit "meaningful set is small" assumption) to block the abuse case in §16 (a malicious
manifest with thousands of fabricated variant lines).

---

## 11. Fleet-wide deduplication

The race: crawler A, B, C discover the same media URL at nearly the same time, potentially
from different machines with no shared state except Redis.

Because `discovery_id` (§3) is a deterministic function of the normalized URL, **no
existence check is required before the write** — unlike the frontier's `urls:known` SET
check, which is needed because frontier job identity is the raw URL string in a SET, not a
derived key. Here, the write itself is naturally idempotent:

- Asset hash fields set via `HSETNX` (first writer wins on `first_seen`/immutable fields;
  every writer's `last_seen`/observation count updates via `HSET`/`HINCRBY` regardless of
  who "won").
- Job creation guarded so a job is only enqueued **once**, on first discovery of a
  previously-unseen asset — re-discovery of an asset with an existing job (in any state,
  matching current SQLite's `ON CONFLICT` no-op-on-existing-job behavior) never creates a
  second job.

All of this happens inside **one Lua script per `record_media_link` call** — this is
strictly better than today's SQLite path, which is three separately-committed statements
(bug #3, §1). Atomicity requirement: the entire "upsert asset fields + append/cap
observation + conditionally create job" sequence must be one script; nothing here needs
cross-key coordination beyond what a single Lua script naturally provides, since every
piece of state for one asset lives under keys derived from the same `discovery_id`.

Result: **one asset, many observations** — confirmed as the correct model (matches §2's
entity analysis; no scenario examined in this investigation justifies per-crawler asset
copies).

---

## 12. Redis keyspace design

Namespace convention follows the frontier's `{namespace}:...` pattern
(`core/redis_frontier.py`, confirmed via `config.yaml`'s `redis_namespace: "crawler"`), but
media evidence gets its **own namespace**, not a sub-path of the frontier's, per the
instruction to keep `crawler:*` and `evidence:*` separated. Proposed config: a new
`media_evidence.redis_namespace` (default `"evidence"`), independent of
`frontier.redis_namespace` (default `"crawler"`), sharing `redis_host`/`redis_port` by
default (same physical Redis, logically separated namespaces) but overridable to a
different `redis_db`/instance entirely if operational tuning (§14 persistence
requirements) diverges enough to warrant it.

`{ns}` below = `media_evidence.redis_namespace`, `{aid}` = the asset's `discovery_id` hex
digest.

| Key | Type | Fields / Members | Purpose | Writer | Reader | TTL |
|---|---|---|---|---|---|---|
| `{ns}:asset:{aid}` | HASH | canonical_url, media_type, source_domain, mime_type, status (read-through from job/result — §1a/§2), first_seen, last_seen, content_id, match_confidence, matched_title, observation_count | Asset identity + evidence description | crawler (create + `last_seen`/`observation_count` only), job/result transitions (status/match fields — never rediscovery) | crawler, fingerprinter, CLI/tools | none (evidence durability via Redis AOF/RDB — §14; memory strategy §15) |
| `{ns}:asset:{aid}:observations` | LIST (JSON entries), capped via `LTRIM` | last N `{source_page, referrer_url, discovered_by, discovery_method, mime_type, content_length, observed_at}` | Recent provenance detail | crawler | investigator tooling, fingerprinter (needs latest `source_page`) | none, size-capped instead |
| `{ns}:asset:{aid}:variants` | HASH, capped count | `variant_url → {bandwidth,resolution,codecs}` | Manifest variant metadata | crawler | fingerprinter (pick representative rendition) | none, size-capped |
| `{ns}:content:{content_id}:assets` | SET | asset ids sharing a content identity | Cross-asset linkage (§3) | fingerprinter | investigator tooling | none |
| `{ns}:jobs:queue` | ZSET | `aid → priority*C + seq` | Global priority job queue | crawler (on new-asset job creation) | fingerprinter (claim) | n/a |
| `{ns}:jobs:seq` | STRING (counter) | monotonic `INCR` | Tiebreak for queue ordering | crawler | claim script | n/a |
| `{ns}:job:{aid}` | HASH | status, priority, retry_count, error_class, last_error, byte_range_strategy | Job operational state | crawler (create), fingerprinter (transitions), recovery sweep | fingerprinter, recovery sweep, CLI | short TTL after `completed`/`permanent_failure` (§14) |
| `{ns}:jobs:claim:{aid}` | HASH | token, worker_id, claimed_at | CAS ownership record | claim/renew scripts | renew/complete/fail scripts | n/a (deleted on completion/reclaim) |
| `{ns}:jobs:inflight` | ZSET | `aid → lease_expiry_epoch` | Lease-expiry detection | claim/renew scripts | recovery sweep | n/a |
| `{ns}:jobs:retry_scheduled` | ZSET | `aid → eligible_retry_epoch` | Backoff scheduling | fail/reclaim scripts | recovery sweep (promotes back to `jobs:queue`) | n/a |
| `{ns}:jobs:permanent_failure` | SET | asset ids | Terminal failure record | recovery sweep, fail script | CLI/tools | capped/monitored (§16 abuse note) |
| `{ns}:result:{aid}` | HASH | dinov2_similarity, phash_score, audio_score, temporal_verified, aggregate_decision, confidence, algorithm_versions, worker_id, processed_at | Durable fingerprint evidence | fingerprinter (complete) | investigator tooling, feedback consumer | none (evidence durability via Redis AOF/RDB — §14; no external store) |
| `{ns}:events:confirmed_match` | STREAM | `{asset_id, source_domain, matched_title, confidence, processed_at}` | Decoupled crawler-feedback channel (§19) | fingerprinter (on confirmed completion) | domain-scoring consumer | trimmed (`XTRIM` to a bounded length/age) |

Atomic operations required: asset creation/update (§11), claim, renew, complete/fail,
reclaim sweep — all single Lua scripts, matching the frontier's rule that every mutation is
one round trip.

---

## 13. Queue design

A single global priority `ZSET` (`{ns}:jobs:queue`, score = `priority * C + seq` — same
shape as the frontier's per-domain queues), **not** sharded per domain or per media type.
Justification: fingerprint jobs have no per-domain fairness requirement (§6); a single
`job_type` field defaulting to `"fingerprint"` is included on the Job hash for forward
compatibility, but multiple queues are not warranted by anything in the current system —
adding them now would be exactly the "complicated scheduling system" the brief says to
avoid absent a concrete requirement.

**This is explicitly the initial architecture, not a permanent ceiling.** No sharding,
domain queues, or multiple queues should be introduced now. If the fingerprint fleet grows
large enough that queue contention becomes a measured problem (§24's hot-key watchpoint),
the evolution should be telemetry-driven at that point — the same pattern the frontier's own
deferred "eligible-domain-index" redesign already establishes as this codebase's convention
(fork A's research, §6): validate the need with real numbers first, then shard; don't
pre-shard speculatively.

- **Priority**: preserved from current behavior (`record_media_link(priority=...)`,
  already passed by every crawler call site with different values depending on discovery
  method).
- **FIFO within priority**: `seq` tiebreak, identical mechanism to the frontier's
  `{ns}:seq` counter.
- **Retry backoff / delayed jobs**: both naturally expressed as ZSET scores — a
  `retry_scheduled` entry is just a job whose "queue" is a different ZSET keyed by
  eligible-timestamp instead of priority, promoted back to `jobs:queue` once due, exactly
  mirroring the frontier's `retry_scheduled → domain queue` promotion in
  `reclaim_and_promote`.

---

## 14. Durability — Redis role decision (revised)

**This section changed materially in the revision pass.** The first version of this
document chose a hybrid model that routed confirmed-match evidence into the existing
SQLite file as a "one-way terminal-event archival export," reasoning that a narrow,
one-shot export wasn't really a "mirror." That reasoning is withdrawn: whatever it's
called, it puts a synchronous or near-synchronous SQL write on the production
fingerprint-completion path for the single highest-value event this subsystem produces,
which is precisely the kind of production SQL dependency the Architecture Boundaries
section forbids outright, independent of how the write is scoped or framed. Corrected
decision below.

**Redis is the sole production durability boundary for this phase**, for every category of
Media Evidence state: assets, observations, fingerprint jobs, claims, retries, fingerprint
results, and confirmed matches. There is no external store in the production path. Within
that boundary, the design distinguishes two different durability needs and meets each with
Redis's own persistence mechanisms rather than reaching outside Redis for either:

**Operational recoverability** — can the system pick up where it left off after a restart,
without necessarily preserving every last in-flight detail? Applies to `jobs:queue`,
`job:{aid}`, `claim:{aid}`, `inflight`, `retry_scheduled`. Worst-case loss (e.g. a hard
crash between AOF fsyncs) means an in-flight fingerprint job silently disappears from the
queue; the underlying asset, if still discoverable, gets naturally re-enqueued the next
time a crawler revisits a source page that still contains the link — self-healing, the same
philosophy the frontier already relies on for its own in-flight claims. The cost of losing
one is higher here than in the frontier, though: unlike a lost URL fetch (cheap to redo), a
lost fingerprint job can waste real compute already spent (a completed download, partial
GPU inference) — an argument for *tighter* persistence settings than the frontier needs,
not for stepping outside Redis.

**Evidence durability** — must the record of what was found survive a restart with no gaps,
because it's real anti-piracy work product, not a reconstructable intermediate? Applies to
`asset:{aid}`, `asset:{aid}:observations`, `asset:{aid}:variants`, `result:{aid}`, and
especially any asset/result carrying `aggregate_decision = confirmed`. This is a stronger
requirement than operational recoverability, but it is still met **inside Redis**, via
Redis's own persistence guarantees — not by writing the data twice into two different
systems.

**Recommended Redis persistence configuration for the evidence Redis instance/DB** (stronger
than what the frontier requires, and — per the Architecture Boundaries section — a separate
physical/logical instance from the frontier's Redis, so this tuning doesn't have to be
traded off against frontier throughput):
- **AOF enabled**, `appendfsync everysec` at minimum for operational state; `always` is
  worth evaluating specifically around the confirmed-match write path once real write
  volume is known (§22 benchmark), trading a small amount of throughput for a tighter
  data-loss window on the highest-value event.
- **Periodic RDB snapshots** in addition to AOF (`save` points), as a fast-restart path and
  a second recovery mechanism if the AOF itself is ever corrupted.
- **Off-instance backup of the AOF/RDB files** (standard Redis operational practice — copy
  persistence files to separate storage on a schedule) so a total loss of the Redis host
  itself doesn't equal a total loss of evidence. This is an operational/infra practice, not
  a second application-level datastore — no application code writes to or reads from the
  backup location; it exists purely for disaster recovery of the one production store.
- `maxmemory-policy=noeviction` (already recommended in §15) so memory pressure fails loud
  rather than silently discarding evidence that hasn't been snapshotted yet.

**Explicitly out of scope for this phase, not decided now:** whether Media Evidence ever
needs long-term archival storage *beyond* Redis (e.g. cold storage, a data warehouse export,
a compliance retention store) is a **future, separate architecture decision** — flagged
here as a known open question, deliberately not resolved, and specifically **not
pre-selected to be SQLite** or any other store. If and when that need becomes concrete, it
gets its own design pass with its own tradeoffs, not a default inherited from this
document.

---

## 15. Memory and scale estimate

Rough, order-of-magnitude sizing (Redis hash/ZSET/list overhead is real but small per
entry; figures below are deliberately conservative, not a benchmark result):

| Scenario | Structure | Rough per-record cost | Total |
|---|---|---|---|
| 1,000,000 assets | `asset:{aid}` HASH (~10 fields) | ~400–600 B | ~0.5 GB |
| 1,000,000 assets × ~10 retained observations each (capped) | `asset:{aid}:observations` LIST | ~150 B/entry (JSON) | ~1.5 GB |
| ~15% of assets are manifests × ~10 variants | `asset:{aid}:variants` HASH | ~100 B/entry | ~0.15 GB |
| 1,000,000 fingerprint jobs (job hash + claim hash + queue ZSET entry, in aggregate across their lifecycle) | 3 structures | ~250–300 B combined | ~0.3–0.9 GB |
| 1,000,000 results (completed subset) | `result:{aid}` HASH | ~300 B | ~0.3 GB |

**Ballpark total at this scale: 3–5 GB.** Recommend sizing the dedicated evidence Redis
instance/DB with headroom well above this (8–16 GB) and `maxmemory-policy=noeviction` —
evidence must never be silently dropped by an eviction policy; if memory pressure becomes
real, that must surface as an alert and a deliberate retention-policy change (e.g. shrink
the observation cap), not silent data loss.

**Growth hazards** (feed directly into §16's mitigations): attacker-controlled URL length
(no current cap anywhere in `URLUtils.clean_media_url`), unbounded observation flooding
absent the cap in §4, unbounded manifest variant counts absent the cap in §10, retry
amplification absent `max_retries` enforcement (§8), and result/job history accumulating
forever absent the TTL policy in §12/§14.

---

## 16. Security / abuse considerations

Media URLs and all metadata originate from untrusted, adversarial websites (this crawler's
entire purpose is to crawl piracy sites). Mitigations to **enforce in the eventual
implementation** (documented here, none implemented in this design phase):

- **URL length cap** — reject before ever computing a Redis key or making a round trip
  (cheapest possible defense point; currently no length limit exists anywhere in
  `URLUtils`).
- **Observation flooding** — the capped ring buffer + exact counter (§4) bounds Redis
  growth; additionally recommend a soft per-source-domain observation-rate threshold
  (alert/throttle, not implemented here) since flooding is also a *signal* worth
  surfacing, not just a cost to absorb.
- **Duplicate-asset flooding via URL-parameter variation** — because `discovery_id` is
  URL-derived (§3), a malicious page generating thousands of URLs that differ only in an
  unstripped query parameter (deliberately defeating dedup) creates thousands of distinct
  *real* assets, not a dedup bug — the mitigation is a **per-source-domain new-asset-creation
  rate cap/circuit breaker** (e.g., throttle further job creation from a domain that has
  produced an anomalous number of distinct assets in a short window), analogous in spirit
  to the frontier's per-domain rate gate but triggered on asset-creation rate rather than
  claim rate.
- **Job flooding / retry amplification** — bounded by `max_retries` → `permanent_failure`
  (§8); `permanent_failure` set itself should be monitored/capped since it's still
  attacker-influenced growth.
- **Metadata size** — truncate/reject oversized `mime_type`, `matched_title`, `last_error`
  strings before any `HSET` (unbounded error strings from a malformed FFmpeg failure are a
  realistic vector).
- **Manifest variant flooding** — capped variant count per manifest (§10), independent of
  what a malicious manifest actually contains.
- **Redis memory exhaustion generally** — `noeviction` + monitoring (§15) so exhaustion
  fails loud (write errors) rather than silently discarding evidence.

---

## 17. API boundary — `MediaEvidenceStore`

The existing five functions named in the brief are a reasonable behavioral surface, but
three of them are missing the arguments a distributed implementation requires, and one
should be decoupled entirely (§19). Proposed interface (Python `Protocol`/ABC, storage-layer
concern — see §23 for placement):

```python
class MediaEvidenceStore(Protocol):
    def record_media_link(self, *, url, source_page, referrer_url, discovered_by,
                           discovery_method, media_type, mime_type, content_length,
                           priority) -> str: ...                      # unchanged shape
    def record_manifest_variants(self, asset_id: str, variants: list[dict]) -> None: ...  # unchanged shape

    def claim_next_fingerprint_job(self, worker_id: str) -> FingerprintJob | None: ...     # now returns a claim token
    def renew_job_lease(self, asset_id: str, token: str) -> bool: ...                      # NEW — heartbeat
    def complete_fingerprint_job(self, asset_id: str, token: str, *, result: FingerprintResult) -> bool: ...  # token added
    def fail_fingerprint_job(self, asset_id: str, token: str, *, error_class: str,
                              last_error: str, retryable: bool) -> bool: ...                # NEW — explicit retry decision (§8)
    def mark_asset_matched(self, asset_id: str, token: str, *, matched_title, confidence) -> None: ...  # domain_database param REMOVED (§19)

    def list_media_assets(self, ...) -> Iterable[dict]: ...           # unchanged shape, paginated for Redis
    def list_observations(self, asset_id: str) -> list[dict]: ...     # SAME METHOD, DIFFERENT CONTRACT — returns capped recent set, not full history (§4). Callers must accept this.
    def list_manifest_variants(self, asset_id: str) -> list[dict]: ...# unchanged shape

    def close(self) -> None: ...
```

- **Unchanged behaviorally**: `record_manifest_variants`, `list_manifest_variants` — no
  distributed semantics needed beyond the atomicity already covered in §11/§12.
- **Same method name, changed contract**: `list_observations` — SQLite returns full
  history; Redis returns the capped recent set (§4) plus `observation_count`. This is a
  deliberate, documented behavior change callers must be updated for, not a silent
  regression.
- **Requiring new distributed semantics**: `record_media_link` (must become one atomic Lua
  operation, fixing bug #3), `claim_next_fingerprint_job`/`complete_fingerprint_job` (must
  carry a claim token, fixing bugs #1/#2).
- **New methods required**: `renew_job_lease` (heartbeat, §7), `fail_fingerprint_job`
  (explicit retry decision, §8 — today's `update_sample_job_status` conflates "fail" with
  "any status change" and takes no retry signal at all).
- **Removed / changed**: `mark_asset_matched`'s direct `domain_database` parameter is
  removed — replaced by the `confirmed_match` event (§12, §19). The store should not import
  or know about `DomainDatabase` at all in the distributed design.

---

## 18. Crawler → evidence boundary

Unchanged in spirit from today's actual behavior (the crawler already doesn't know
anything about fingerprinting internals — verified across all 8 call sites, none of which
reference DINOv2/pHash/audio in any form):

```
crawler engine → record_media_link() / record_manifest_variants() → evidence subsystem
```

The evidence subsystem, not the crawler, decides whether a new job is created (already true
today via the `ON CONFLICT` no-op — preserved in §11's Lua design). No change to this
boundary is required; it was already correctly separated.

---

## 19. Fingerprinter → evidence boundary

No fingerprinter code exists to preserve behavior from — this is new design for a
not-yet-built consumer, kept intentionally minimal:

```
claim_next_fingerprint_job(worker_id) → FingerprintJob (asset_id, token, canonical_url,
                                                          media_type, variants, priority)
        ↓
[fingerprinter downloads media, runs algorithms — entirely outside this subsystem]
        ↓
renew_job_lease(asset_id, token)   [periodic, between pipeline stages]
        ↓
complete_fingerprint_job(asset_id, token, result=FingerprintResult(...))
   or
fail_fingerprint_job(asset_id, token, error_class=..., retryable=...)
```

The fingerprinter never touches `DomainDatabase`, the frontier, or crawler internals
directly — confirming a match only ever writes to `{ns}:result:{aid}` and emits
`{ns}:events:confirmed_match` (§12). A separate, existing-pattern consumer (conceptually
the same role `main.py --mark-match` plays today, minus the direct
`domain_database.add_or_update()` call it currently makes) reads that stream and applies
the score bump. This is the "clean feedback event/API rather than coupling the
fingerprinter directly to frontier/domain internals" the brief asks for.

---

## 20. Failure scenarios walked through

1. **Two crawlers discover the same media simultaneously.** Deterministic `discovery_id`
   (§3) + atomic Lua asset-upsert (§11) → one asset, two observations recorded (bounded per
   §4), zero duplicate jobs (job creation guarded to first-discovery only).
2. **Fingerprinter A claims a job and crashes.** Claim record + `inflight` entry persist
   until lease expiry; recovery sweep (§6) detects it after `fingerprint_lease_ttl`,
   reschedules to `retry_scheduled` (or `permanent_failure` if `retry_count` exhausted).
3. **A's lease expires while it's still legitimately processing (heartbeat missed).**
   Treated identically to a crash by design — this is the conservative, correct default;
   the fix is A calling `renew_job_lease` before the lease window elapses, not the store
   special-casing "probably still alive."
4. **A finishes after its lease was reclaimed.** `complete_fingerprint_job(asset_id,
   A's_stale_token, ...)` fails the CAS check inside the Lua script (B's reclaim already
   overwrote the claim record) → rejected as a no-op, logged, no state mutated. This is the
   entire reason the token exists (§6, §7) — walked through explicitly because it's the
   race the design must get right.
5. **Redis restarts while jobs are queued (not claimed).** With AOF persistence (§14),
   `jobs:queue` is restored intact. Without it, queued jobs are lost but the underlying
   assets remain discoverable on the next crawl pass of their source pages, which
   re-triggers job creation (§11) — degraded but self-healing, matching the frontier's own
   restart philosophy.
6. **Redis restarts while jobs are processing (claimed).** Same AOF dependency; without
   persistence, `inflight`/`claim` records vanish, leaving job hashes whose `status` still
   says `claimed` with no corresponding queue/claim/inflight entry. This is why §14
   recommends AOF specifically for this subsystem (stronger than the frontier needs) rather
   than relying on a reconciliation sweep to paper over it — a slow periodic consistency
   check comparing job-hash status against `inflight` membership is a reasonable belt-and-
   suspenders addition but should not be the primary mechanism.
7. **Fingerprint download fails.** Fingerprinter classifies as `network`/`http_temporary`,
   calls `fail_fingerprint_job(..., retryable=True)` → `retry_scheduled` with backoff (§8).
8. **Fingerprint model fails.** Classified `model_error`, `retryable=True`, same path, with
   a recommended smaller max-retry budget given inference cost (§8).
9. **Same media discovered from 100 source pages.** One asset, `observation_count=100`,
   only the most recent N (§4) retained in detail, exactly one job ever created (§11).
10. **One source generates thousands of duplicate media observations.** Same mechanism as
    #9 at larger scale; the count still grows (real evidence of prevalence) but detailed
    storage stays capped — this is precisely why `observation_count` is tracked separately
    from the capped list rather than being derived from `LLEN` of an unbounded list.
11. **A malicious source generates extremely long media URLs.** Rejected at the API
    boundary before any Redis key is computed (§16) — this must happen in
    `record_media_link`'s validation, before `URLUtils.clean_media_url` even runs the hash.
12. **Fingerprint result says confirmed piracy.** `{ns}:result:{aid}` written with
    `aggregate_decision=confirmed`; asset `status` (read-through, §1a/§2) reflects
    `matched`; `{ns}:events:confirmed_match` event emitted (§19). Durability for this
    highest-value event comes from the evidence Redis instance's own AOF/RDB persistence
    and backup practice (§14), not from a write into a second system — surviving a
    *restart* is covered by that persistence; surviving a *total, unrecovered loss* of the
    Redis host and its backups is explicitly the out-of-scope future archival question
    §14 flags, not something this phase's design claims to solve.

---

## 21. Migration from current SQLite

No data migration performed or planned now (explicit instruction; also, the current
`storage/media_evidence.db` — 5.6 MB on disk today — reflects dev/test-only activity, since
no distributed fingerprinter has ever run against it).

**Preserving existing behavior during transition:**
- The `MediaEvidenceStore` interface (§17) is designed so the 8 existing crawler call sites
  (`crawler/async_crawler.py`, `http_crawler.py`, `tor_crawler.py`, `playwright_crawler.py`,
  `selenium_crawler.py`, `scrapling_crawler.py`, `hybrid_crawler.py`, plus `main.py`'s CLI
  stub) keep calling `record_media_link`/`record_manifest_variants` with the same
  signature. Only the claim/complete/mark-match paths (currently exercised only via
  `main.py`'s CLI stub, not by any production crawl path) change shape, and nothing in the
  crawl hot path is affected.
- **Backend selection**: today, only the *frontier* has a config-driven backend switch
  (`crawler.frontier.type: sqlite|redis`) and no CLI flag at all (confirmed —
  `redis-sqlite-boundary-decision.md` reportedly recommended adding `--redis`/`--sql` flags
  but they were never implemented for the frontier either). Proposed for media evidence: a
  new `config.yaml` key `crawler.media_evidence.type: sqlite|redis` (mirroring the
  frontier's existing convention, for consistency) **plus** an explicit CLI override
  (`--media-backend {sqlite,redis}`, or reuse a shared `--sql`/`--redis` flag if one is
  ever added for the frontier at the same time) — since the brief specifically frames
  SQLite as an independent `--sql` backend, this document recommends closing that
  CLI-flag gap now rather than only matching the frontier's current (arguably incomplete)
  config-only convention.
- **Existing SQLite evidence**: not migrated. Development/testing continues to use
  `SQLiteMediaEvidenceStore` (rename target for today's `MediaEvidenceDatabase` — see
  §23) with its own on-disk file, starting empty in Redis mode. This is consistent with
  "SQLite is not a permanent mirror" — the two backends are independent, not synchronized,
  starting states.
- **Tests**: `tests/media_evidence_test.py`, `tests/fingerprinter_queue_test.py`,
  `tests/streaming_manifest_test.py` currently instantiate `MediaEvidenceDatabase` directly
  and assert on SQLite-specific behavior (e.g., full observation history, not a capped
  list). Under the new interface, these become the `SQLiteMediaEvidenceStore` test suite,
  behaviorally unchanged (a rename, not a behavior migration — proposed separately, not
  performed here per instructions), with a new parallel Redis test suite added alongside,
  mirroring the frontier's existing pattern of separate `url_database`-style vs.
  `redis_frontier_test.py`-style coverage.

---

## 22. Testing strategy (design only — none of this is implemented)

Directly modeled on the frontier's proven test/benchmark shape (`tests/claim_heartbeat_test.py`,
`tests/redis_frontier_test.py`, `tests/crawler_manager_recovery_test.py`,
`tests/benchmarks/*`), adapted for this subsystem's different concurrency shape (no
per-domain fairness, longer-lived leases).

**Unit tests**
- Asset dedup (concurrent `record_media_link` calls for the same URL → one asset).
- Observation creation, capping, and count accuracy under flood.
- Job creation (exactly one job per asset, never re-created on re-discovery of a completed
  asset).
- Claim safety (Lua CAS correctness — token mismatch always rejected).
- Completion, retry (backoff formula, `retry_count` increment), stale completion (§20
  scenario 4, deterministic reproduction).
- Heartbeat / lease renewal (extends `inflight` score; rejects a stale token).
- Lease expiry → recovery sweep → `retry_scheduled` or `permanent_failure` correctly chosen
  based on `retry_count` vs. `max_retries`.

**Multi-worker tests** — 2, 4, 8, 16 concurrent claimers (async or threaded), asserting the
frontier's own core invariant: **zero duplicate successful claims**, ever.

**Multi-process tests** — real OS processes via `multiprocessing` sharing one Redis
instance, matching `tests/benchmarks/distributed_benchmark.py`'s explicit rationale
(catches GIL-masked races that in-process asyncio tests hide).

**Crash tests** — kill a worker (or simulate via never calling complete/renew) after claim,
verify the recovery sweep reclaims after `fingerprint_lease_ttl`, matching
`tests/benchmarks/crash_recovery.py`'s deterministic style (force-expire, call the sweep
directly, assert the stale token's completion is rejected).

**Endurance test** — a synthetic job that runs longer than `fingerprint_lease_ttl` but
heartbeats throughout, asserting it is never reclaimed; a negative control that doesn't
heartbeat and is reclaimed on schedule — directly mirrors
`tests/benchmarks/heartbeat_endurance.py`'s enabled-vs-disabled structure.

**Duplicate-discovery tests** — N simulated crawlers submitting the same URL concurrently,
asserting exactly one asset, N observations (capped per §4), one job.

**Benchmark** (new `tests/benchmarks/media_evidence_benchmark.py`, same manual-CLI-script
convention as the existing benchmark files — not pytest-collected) measuring: asset
insertion/s, observation insertion/s, job creation/s, job claim/s, completion/s, p50/p90/p99
latency (the frontier benchmark's own convention is p90, not p95 — match it for
consistency), duplicate-claim count (must be zero), Redis memory growth, Redis CPU, retry
behavior under induced failure rates.

---

## 23. Package / module structure

The repository's actual conventions (not an idealized structure): all persistence lives
flat under `storage/` (`url_database.py`, `domain_database.py`, `media_evidence_database.py`,
`async_database_writer.py`); the frontier's Redis backend, by contrast, lives in `core/`
alongside crawl orchestration (`core/redis_frontier.py`, `core/frontier.py`,
`core/claim_heartbeat.py`, `core/crawler_manager.py`) — because `core/` in this repo means
"crawl orchestration," not "generic storage backends." `MediaEvidenceDatabase` was
deliberately placed in `storage/`, not `core/`, from the start.

**Proposed placement** (rename proposal only — not performed in this phase, per §21/instructions):

```
storage/
    media_evidence_store.py          # NEW — MediaEvidenceStore Protocol/ABC
    sqlite_media_evidence_store.py   # rename target for storage/media_evidence_database.py
                                      #   (MediaEvidenceDatabase → SQLiteMediaEvidenceStore)
    redis_media_evidence_store.py    # NEW — RedisMediaEvidenceStore, Lua scripts inline
                                      #   or in a sibling storage/media_evidence_lua/ dir,
                                      #   matching core/redis_frontier.py's own style

fingerprinter/                       # NEW top-level package — does not exist today
    worker.py                        # claim → download → fingerprint → complete loop
    downloader.py                    # media download (separate concern from fingerprinting)
    algorithms/
        dinov2.py                    # not implemented — placeholder per README's stated intent
        phash.py
        audio.py

docs/architecture/media-evidence-redis-design.md   # this document
```

Rationale for keeping both evidence-store backends in `storage/` (not mirroring `core/`'s
placement of the frontier's Redis backend): evidence storage is a storage-layer concern by
this repo's own existing precedent (the file already lives there); `core/` here specifically
denotes crawl-loop orchestration, which the evidence store is not part of. Consistency with
already-established convention wins over surface-level symmetry with the frontier's
placement.

`fingerprinter/` is a new top-level package (not nested under `crawler/` or `storage/`)
because it is a genuinely separate concern from all three things the brief warns not to
conflate: evidence storage/coordination, media downloading, and crawler extraction. This
also matches how `discovery/`, `parsers/`, `search_engines/` are already siblings of
`crawler/` and `storage/` at the top level, not nested inside them.

---

## 24. Performance principles (multi-machine, not single-box)

- **Redis round trips per discovered media**: today, 3 separately-committed SQLite
  statements per `record_media_link` call (bug #3, §1). Proposed: **1** Lua round trip
  (§11).
- **Redis operations per fingerprint job**: 1 (claim) + a handful of heartbeats (few,
  since the interval is minutes — §7) + 1 (complete/fail). Recovery-sweep cost is
  amortized background work, not charged per job.
- **Lua scripts required**: claim, renew, complete, fail, reclaim-sweep, asset-upsert — six
  scripts total, each a single atomic unit, matching the frontier's own rule of one script
  per mutation.
- **Hot keys**: `{ns}:jobs:queue` is a single global ZSET under contention from every
  fingerprinter worker claiming and every crawler creating jobs. §6 already argues this is
  an acceptable simplification given fingerprinting's inherently lower QPS (bounded by
  download + ML inference cost, not network fetch cost) — but flagged here explicitly as
  **the one scaling watchpoint to revisit** if job-creation or claim rate ever approaches
  crawl-rate order of magnitude. If that happens, the fix is the same shape as the
  frontier's own deferred "eligible-domain-index" redesign (fork A's research: a lazy,
  telemetry-triggered evolution, not a preemptive one) — shard the queue (e.g. by
  `media_type` or a hash bucket) only once real contention is measured, not speculatively.
- **Memory growth**: §15.
- **Network traffic / fleet scaling**: unlike the frontier (many small, fast operations per
  worker per second), this subsystem is inherently low-frequency per worker (each
  fingerprinter holds one job for minutes) — fleet scale-out here is bounded by *worker
  count* (how many fingerprinting machines exist), not by Redis round-trip budget, which is
  the opposite bottleneck shape from the frontier and should not be judged by the
  frontier's own single-machine throughput benchmarks.

Python-level micro-optimization is explicitly out of scope for this phase, per instructions
— architectural cost (round trips, atomicity boundaries, key contention) is what's analyzed
above.

---

## 25. Final recommendation

```
MEDIA SUBSYSTEM NAME:        Media Evidence (MediaEvidence / MediaEvidenceStore) — not "MediaHandler"
STORAGE INTERFACE:           MediaEvidenceStore (Protocol/ABC) in storage/media_evidence_store.py
PRODUCTION BACKEND:          RedisMediaEvidenceStore (storage/redis_media_evidence_store.py)
DEVELOPMENT BACKEND:         SQLiteMediaEvidenceStore (rename of today's MediaEvidenceDatabase)
ASSET IDENTITY:               discovery_id = sha256(URLUtils.clean_media_url(url)), deterministic,
                              zero-coordination, FIXED by this design; content_id is a deliberately
                              abstract, optional, later-populated cross-reference (§3) — not assumed
                              to be any one specific algorithm's output; used to LINK assets, never
                              to merge them
OBSERVATION MODEL:            exact HINCRBY counter + capped recent-observation ring buffer, cap size
                              configurable via `max_observations_per_asset` (initial default 20,
                              to be tuned from real benchmark data, §22) — never unbounded history
JOB MODEL:                   one job per asset (UNIQUE invariant preserved); states
                              queued → claimed → completed | retry_scheduled → queued | permanent_failure
                              (§5a: `sampled`/`hashed` from current SQLite are confirmed-unreachable
                              placeholders, intentionally dropped, not migrated)
CLAIM MODEL:                 single global priority ZSET + atomic Lua claim (frontier's proven
                              pattern), NOT domain-sharded — this is the initial architecture and an
                              explicit, telemetry-driven scaling watchpoint (§6, §13, §24), not a
                              permanent ceiling
LEASE MODEL:                  fingerprint_lease_ttl — configurable, initial default 900s (order of
                              magnitude longer than the frontier's 90s; fingerprinting is minutes,
                              not milliseconds) — tune from real pipeline timing once it exists
HEARTBEAT MODEL:              fingerprint_heartbeat_interval — configurable, defaults to lease_ttl/3,
                              always clamped below lease_ttl (the actual invariant, §7) so renewal
                              margin can never be misconfigured away; opportunistic renewal between
                              pipeline stages, same run_with_heartbeat/ClaimLostError mechanism as
                              the frontier
RETRY MODEL:                  store applies generic backoff/permanent-failure machinery; RETRYABILITY
                              is decided by the fingerprinter and passed in explicitly, never inferred
                              from an error string by the store
RESULT MODEL:                 separate FingerprintResult from Job — durable evidence (scalar scores,
                              decision, confidence, versions) never large binaries/embeddings
MANIFEST MODEL:               unchanged from current behavior — variants are bounded descriptive
                              metadata on the manifest asset, never independently fingerprinted
REDIS KEYSPACE:               {media_evidence.redis_namespace}:... (default "evidence"), fully
                              separate from the frontier's {frontier.redis_namespace}:... ("crawler")
DURABILITY MODEL:             Redis is the SOLE production durability boundary (revised, §14) — no
                              external store, no SQLite in the production path. Operational
                              recoverability (queue/claim/inflight/retry) vs. evidence durability
                              (assets/observations/results/confirmed matches) are distinguished, both
                              met via Redis's own AOF + periodic RDB + off-instance backup, tuned
                              tighter than the frontier needs. Long-term archival beyond Redis is an
                              explicit future out-of-scope decision, not pre-selected to be SQLite.
REDIS MEMORY STRATEGY:        noeviction + explicit caps everywhere abuse/flooding is possible
                              (observations, variants, URL length, metadata size, retry count);
                              alert on pressure rather than silently evict evidence
CRAWLER → EVIDENCE API:       unchanged — record_media_link()/record_manifest_variants(), same
                              signatures, crawler never learns about fingerprinting internals
EVIDENCE → FINGERPRINTER API: claim_next_fingerprint_job/renew_job_lease/
                              complete_fingerprint_job/fail_fingerprint_job — token-CAS everywhere
FINGERPRINTER → CRAWLER FEEDBACK: {ns}:events:confirmed_match Redis Stream, consumed by a
                              decoupled domain-scoring consumer — fingerprinter never calls
                              DomainDatabase directly (today's mark_asset_matched() does; removed)
MULTI-MACHINE MODEL:          any crawler machine can independently compute the same asset key with
                              zero coordination (discovery_id is pure function of the URL); any
                              fingerprinter machine claims from one shared global queue
FAILURE MODEL:                walked through in full in §20; the load-bearing invariant is the
                              claim-token CAS check on every mutating operation
TEST STRATEGY:                unit + multi-worker + multi-process + crash + endurance +
                              duplicate-discovery + benchmark, directly mirroring the frontier's
                              already-proven test/benchmark shape (§22)
MIGRATION STRATEGY:           no data migration; MediaEvidenceStore interface keeps crawler call
                              sites stable; new config.yaml `media_evidence.type` + CLI override,
                              closing the CLI-flag gap the frontier itself never closed
```

---

## Appendix: open questions for the implementation phase (not decided here)

- Exact `max_retries` / `max_retries_model` defaults — needs real pipeline timing data,
  not guessed here.
- Whether `job_type` (included for forward compatibility, §13) ever needs more than one
  value — no current requirement demands it; do not build multi-queue support speculatively.
- Whether the per-source-domain new-asset-creation circuit breaker (§16) should share
  infrastructure with the frontier's own domain-level rate gating, or be fully independent
  — a reasonable question, deliberately left open rather than pre-decided, since it depends
  on operational experience this subsystem doesn't have yet.
- Whether long-term archival of evidence *beyond* Redis is ever required, and if so what
  store it should use — explicitly deferred as future, out-of-scope work (§14, revised);
  not pre-selected to be SQLite or anything else.
- Whether `permanent_failure` should ever be automatically reopened by rediscovery (§5a) —
  this document's working recommendation is no, but it's flagged as needing product
  sign-off, not settled by architectural inference alone.
- The concrete shape of `content_id` (§3) — single cryptographic hash, single perceptual
  fingerprint, or a composite of several typed references — is left for whoever designs the
  fingerprinter pipeline; Media Evidence commits only to having a slot and a generic linking
  mechanism for it.
- Exact Redis AOF `appendfsync` policy for the confirmed-match write path specifically
  (`everysec` vs. `always`, §14) — a throughput/durability tradeoff that should be decided
  from real write-volume benchmark data (§22), not guessed here.
