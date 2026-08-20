# Crawler Network Failure Handling Audit

## Status

AUDIT ONLY — NO CODE CHANGED

## 1. Incident Context

**OBSERVED FROM RUN DATA** (`benchmark/results/overnight_e2e_crawler.json`, generated
`2026-08-13T01:59:11Z`):

- Run window: `2026-08-12T17:07:40Z` → `2026-08-13T01:59:11Z` (`duration_seconds: 31891.56`, ≈ 8h51m32s).
- `crawl_engine: "auto"`, `backend: "redis"`, `worker_count: 25`.
- Counts (this run): `discovered_unique: 22359`, `visited_unique: 47`, `failed_permanent_unique: 22189`,
  `skipped_unique: 123`, `retry_scheduled_current: 0`, `queued_current: 0`, `inflight_current: 0`.
- `configuration.crawler.frontier`: `max_retries: 3`, `base_backoff: 5.0`, `max_backoff: 300.0`,
  `lease_ttl: 90.0`, `recovery_enabled: true`, `recovery_interval: 30.0`.
- The JSON's own `failures` section states verbatim: *"The crawler records aggregate
  visited/failed/skipped outcomes only; it does not categorize failures by HTTP-status vs.
  fetch-exception vs. extraction-error, so those subcategory breakdowns are unavailable
  (reporting fabricated subcategories was ruled out)."* This is the report generator's own
  admission of the observability gap analyzed in §9.

**INFERENCE (user-reported, not in run data):** the user's Internet connection was lost at an
unknown point during this run. The exact outage start/end time is **not determinable from the
current implementation/evidence** — nothing in the frontier, crawler, or report records local
network state.

## 2. Current Failure Path

Traced with `crawler.engine: "auto"` (the config.yaml and benchmark-recorded value for this
incident), which routes through `HybridCrawler`, not `AsyncCrawler` directly. All file/line
references are current source.

| Step | File / Function | Condition | Resulting state |
|---|---|---|---|
| 1. Claim | `core/redis_frontier.py`, `claim_next` Lua script (~L200-254) | Domain rate-gate elapsed, URL at head of a domain queue | `INCR ns:attempts:<url>` → `attempt`; `HSET ns:claim:<url>` with `token`, `attempt`; `ZADD ns:inflight` with `now+lease_ttl` |
| 2. Dispatch | `core/crawler_manager.py` `CrawlerManager.__init__` (L211-212) | `crawler.engine == "auto"` | `self._crawler = HybridCrawler(...)` |
| 3. Schedule/queue | `crawler/hybrid_crawler.py` `scheduler()` (L393-421), `worker()` (L283-391) | claim dequeued | `run_with_heartbeat(self.frontier, claim, self._run_engine_plan(url), ...)` |
| 4. Engine plan | `crawler/hybrid_crawler.py` `_run_engine_plan()` (L225-281), `core/crawler_router.py` `CrawlerRouter.get_engine_plan()` (L129-174) | initial plan for a non-onion URL: `["async", "scrapling"?, "playwright", "selenium", "http"]` | each engine tried in turn within **one** claim/heartbeat span |
| 5. Fetch (async engine) | `crawler/async_crawler.py` `AsyncCrawler.fetch()` (L69-146) | `aiohttp` request; loop `for attempt in range(1, self.max_retries+1)` (engine-local `max_retries=3`, distinct counter from the frontier's) | any non-`CancelledError` exception → `except Exception as e: last_error = str(e); ...; await asyncio.sleep(1)`; after 3 internal attempts, returns `(None, last_error)` |
| 6. Escalation decision | `core/crawler_router.py` `needs_browser_upgrade()` (L85-127) | `failure_reason` lower-cased and matched against a fixed token list (`captcha`, `cloudflare`, `403`, `429`, `javascript`, …) | a generic connection/DNS/timeout exception string matches **none** of these tokens → `needs_browser_upgrade` returns `False` → plan falls through to `fallback_order["async"] = [scrapling, playwright, selenium, http]` (or without scrapling if disabled) |
| 7. Escalation loop | `crawler/hybrid_crawler.py` `_run_engine_plan()` (L242-281) | every remaining engine in the plan attempted, each with its own internal fetch/retry loop (`HTTPCrawler.fetch`, `crawler/http_crawler.py` L67-120, same `except Exception as exc: last_error = str(exc)` pattern) | plan exhausted, all attempted; loop exits with `html=None`, `failure_reason=<last engine's message>` |
| 8. Frontier completion | `crawler/hybrid_crawler.py` `worker()` (L336-342) | `failure_reason` truthy | `status = "failed"`; `await self.frontier.mark_failed(claim, failure_reason or "")` |
| 9. Retry-vs-terminal decision | `core/redis_frontier.py` `_complete()` (L547-590) → `_complete_claim_script` Lua (L258-317) | `attempt` (the value stamped at claim time, step 1) compared to `max_retries=3` | `attempt < 3` → `ZADD ns:retry_scheduled` with `backoff = base_backoff * 2^(attempt-1)` (5s, then 10s), capped at `max_backoff=300`; `attempt >= 3` → `SADD ns:urls:failed_permanent`, `DEL ns:attempts:<url>`, `DEL`/`EXPIRE ns:meta:<url>` (terminal) |
| 10. Recovery/promotion | `core/crawler_manager.py` `_recovery_loop()` (L502-534, every `recovery_interval=30s`) → `reclaim_and_promote` Lua (`core/redis_frontier.py` L341-...) | retry's backoff timestamp has elapsed | URL re-added to its domain queue → re-claimed at step 1, `attempt` incremented again |
| 11. Terminal | step 9, `attempt >= max_retries` branch | 3rd claim also fails | URL is in `ns:urls:failed_permanent`; no code path reads this set to requeue it (verified — see §5) |

**VERIFIED FROM SOURCE:** steps 5-8 (the engine fetch/escalation layer) never call anything in
`core/redis_frontier.py` and have no concept of `FrontierUnavailable`. `FrontierUnavailable` is
raised only when the **Redis connection itself** (the frontier backend) is unreachable — not
when the crawler cannot reach a **target website**. These are two structurally different failure
classes flowing through the same `mark_failed(claim, error)` call at step 8, but only the
Redis-unreachable class has an exception type carved out for it.

## 3. Failure Classification

**VERIFIED FROM SOURCE:**

| Category | Distinguished as a distinct state? | Evidence |
|---|---|---|
| HTTP 404/403/429/500/503 | Only as a formatted string `f"HTTP {status}"` (`async_crawler.py` L93, `http_crawler.py` L77) | 5xx gets one bonus in-engine retry (`response.status >= 500 and attempt < max_retries`); all others return immediately |
| Target connection refused / target DNS failure / target timeout | No — falls into the generic `except Exception as e: last_error = str(e)` branch (`async_crawler.py` L140-143, `http_crawler.py` L118-122) | exception class and errno are discarded; only `str(e)` is kept |
| Local interface down / Internet disconnected / default route unavailable / local DNS resolver unreachable | No — **identical code path** to the line above | Same `except Exception` catches both a single dead target and a total local outage; no signal anywhere distinguishes "this host has no route to the Internet" from "this one target is unreachable" |
| Proxy/TLS/network-stack failure | No — same generic exception branch | same as above |

**Conclusion (VERIFIED FROM SOURCE):** every non-HTTP-response failure — target-specific or
local-infrastructure — collapses into the same generic `Exception` branch and is represented
downstream only as `failed_permanent` with no stored reason. The codebase does not currently
have *any* concept of a local/infrastructure failure category as distinct from a target failure.

## 4. Retry Semantics

**VERIFIED FROM SOURCE:**

- **What consumes a frontier retry:** one `mark_failed(claim, ...)` call, i.e. one full pass
  through the entire engine-escalation chain (`_run_engine_plan`) coming back with no HTML.
  Individual engine-internal retries (e.g. `AsyncCrawler.fetch`'s own `for attempt in
  range(1, max_retries+1)` loop, `max_retries=3` at the engine level) do **not** each consume a
  frontier retry — they all happen inside the one claim/attempt.
- **What does not consume a retry:** `mark_skipped` (blacklisted URLs, `hybrid_crawler.py`
  L293-298) is terminal and bypasses the retry/backoff branch entirely; a `FrontierUnavailable`
  raised while completing a claim propagates and is caught by the worker's own
  `except FrontierUnavailable` (L373-378), which explicitly does **not** call `mark_failed` —
  the claim is abandoned for lease-based reclaim instead, so a Redis-outage failure does not
  consume a retry the way a fetch failure does.
- **Where max retries is enforced:** `core/redis_frontier.py`, inside the
  `_complete_claim_script` Lua (`attempt < max_retries` branch, L306-314) and identically inside
  `_reclaim_and_promote_script` (L372-381) for abandoned/lease-expired claims.
- **Per URL or per attempt:** per URL. `ns:attempts:<url>` is a single counter keyed by URL,
  incremented on every claim (`INCR`, claim script L236) regardless of which prior attempt(s)
  failed or why. It is not scoped to a worker, host, or failure type.
- **Backoff:** exponential, `base_backoff * 2^(attempt-1)`, capped at `max_backoff`. With this
  incident's config (`base_backoff=5.0`, `max_retries=3`): attempt 1 fail → 5s backoff; attempt 2
  fail → 10s backoff; attempt 3 fail → `failed_permanent`. Total scheduled backoff before
  terminal failure is **15 seconds**, plus however long the 30s `recovery_interval` sweep takes
  to notice each due retry and re-queue it (§2 step 10) — so wall-clock time from first claim to
  `failed_permanent` for a URL is on the order of tens of seconds to ~2 minutes under continuous
  failure, not minutes-to-hours.
- **After retry exhaustion:** `finalize_terminal(ns:urls:failed_permanent)` — `SADD` into the
  terminal set, delete the attempts counter, delete (or TTL-expire, if `terminal_meta_ttl_seconds`
  is configured — it is **not** set in `config.yaml`, so it defaults to `0` and the meta hash is
  deleted immediately, `core/redis_frontier.py` L117, L287-291) the per-URL metadata.
- **Exact method causing permanent failure:** `RedisURLFrontier.mark_failed()` →
  `RedisURLFrontier._complete()` → the `_complete_claim_script` Lua script's `else` branch
  (L311-314), when invoked with `attempt >= max_retries`.
- **Does an infrastructure outage consume the same retry budget as a genuine target failure?**
  **Yes, verified from source.** Nothing between `AsyncCrawler.fetch`'s generic `except
  Exception` and the Lua script's `attempt < max_retries` comparison inspects *why* the fetch
  failed. A connection-refused from one dead target and a connection-refused from a total local
  outage both produce a `str(exception)` that is never classified before reaching
  `mark_failed`.

## 5. Redis Frontier Semantics

**VERIFIED FROM SOURCE** (`core/redis_frontier.py`):

- **Claim state:** `ns:claim:<url>` hash (`token`, `attempt`, `domain`, `priority`,
  `claimed_at`) + membership in the `ns:inflight` ZSET scored by lease expiry.
- **Retry state:** membership in the `ns:retry_scheduled` ZSET, scored by `now + backoff`.
  Promoted back to a domain queue by `reclaim_and_promote` once due.
- **Permanent failure state:** membership in the `ns:urls:failed_permanent` SET (a `SADD`, no
  further metadata attached once `terminal_meta_ttl_seconds` expires/is unset).
- **CAS/atomicity:** the retry-vs-terminal decision and the state transition are one atomic Lua
  script (`_complete_claim_script`) keyed by matching `claim.token` against the current
  `ns:claim:<url>` token — stale/superseded claims are rejected (`return 'stale'`), so this part
  of the design is race-safe.
- **Can a URL be safely returned to `retry_scheduled`/deferred state?** Mechanically, yes — the
  Redis primitives for "not now, try again later" already exist and are exactly what powers the
  existing 3-attempt retry ladder. **What does not currently exist is any code path that ever
  moves a URL *out of* `ns:urls:failed_permanent` back into `retry_scheduled` or a domain
  queue.** `failed_permanent` is verified (by grep across `core/redis_frontier.py`) to be
  write-only on the crawl-time hot path: the only reads of that set are `SCARD` calls inside
  `get_status_counts()` / `get_frontier_snapshot()`-style methods (L693-765) for reporting
  purposes. No `SREM`/`SMEMBERS`/`SPOP` against `ns:urls:failed_permanent` exists anywhere in the
  crawler codebase, and `--unfinished` (`CrawlerManager.load_unfinished_urls`,
  `core/crawler_manager.py` L380-396) reads **SQLite** `url_database` statuses
  (`"queued"`/`"pending"`), which — per `_sql_mode_mirror` (`hybrid_crawler.py` L65,
  `async_crawler.py` L55) — is **not populated at all when the Redis frontier is active**. So for
  this incident's configuration (`frontier.type: "redis"`), there is no existing recovery path,
  automatic or CLI-driven, that returns a `failed_permanent` URL to circulation.
- **Does the existing mechanism already support the required semantics?** The retry/backoff
  *mechanism* is reusable (§9 discusses this). What is missing is (a) a way to distinguish which
  failures should even be allowed to consume the existing retry ladder, and (b) any path back out
  of the terminal set. Both are gaps, not present today — this audit does not propose a new Redis
  data structure to fix them; §9/§13 note that the existing ZSET-based retry ladder already
  provides most of the needed primitive.

## 6. URL-Loss Mechanism

**VERIFIED FROM SOURCE — the sequence in the audit brief is possible as written:**

```
Internet disappears (local)
  -> every "async"/"http"/"playwright"/"selenium"/"scrapling" fetch raises a generic
     Exception (async_crawler.py fetch(), http_crawler.py fetch(), by pattern likely
     the other engines too — see "not fully inspected" note below)
  -> caught by `except Exception as e: last_error = str(e)` with no classification
  -> HybridCrawler._run_engine_plan() escalates through the full engine chain because
     needs_browser_upgrade() finds none of its recognized tokens in a raw connection
     error string, then exhausts the plan
  -> worker() calls frontier.mark_failed(claim, failure_reason)
  -> RedisURLFrontier._complete_claim_script: attempt < max_retries(3) -> retry_scheduled
     (5s, then 10s backoff)
  -> recovery loop (30s cadence) promotes the due retry back to a domain queue
  -> re-claimed, attempt increments, fails again (Internet still down)
  -> on the 3rd failed attempt: attempt >= max_retries -> SADD urls:failed_permanent
  -> URL is in a set that nothing in the crawl-time or CLI code path ever reads to requeue
```

This chain is fully supported by source evidence in §2, §4, and §5. **Why the architecture
cannot currently distinguish "this website is dead" from "our machine has no Internet":**

1. The only signal available at the point of failure is `str(exception)` (§3) — exception class,
   errno, and socket-level detail are discarded before the string ever reaches the retry/backoff
   decision.
2. The escalation heuristic that exists (`needs_browser_upgrade`, §2 step 6) inspects that
   string for **target-side** signatures (CAPTCHA, Cloudflare, 403/429, "javascript required").
   It has no local-infrastructure vocabulary at all (no check for e.g. "Name or service not
   known", "Network is unreachable", "Temporary failure in name resolution" as a *class*, as
   opposed to matching one target's specific block page).
3. `RedisURLFrontier`'s retry ladder is keyed purely by `attempt` count per URL — it has no
   input describing whether the *previous* failure was target- or infrastructure-caused, so it
   applies the identical 3-attempt/5s-10s ladder regardless.
4. There is no process anywhere (verified by grep, §7) that samples whether the local host can
   reach the Internet at all, so no signal exists that *could* have been consulted even if the
   retry logic wanted to ask "is this our fault?"

This is not a bug in the sense of violating a documented contract — no current design document
claims the crawler distinguishes these two cases. It is an absent capability.

## 7. Existing Network Health / Recovery

**VERIFIED FROM SOURCE (grep across `crawler/`, `core/`, `utils/`):** no connectivity check,
health check, circuit breaker, or "network status" concept exists anywhere in this codebase.

The only "outage" and "recovery" machinery that exists is for the **Redis frontier backend
itself becoming unreachable** — a materially different failure class (see below), extensively
designed and tested:

- `FrontierUnavailable` (`core/frontier.py` L12-25) — raised only from `RedisURLFrontier` methods
  when the Redis connection/operation fails.
- `CrawlerManager._recovery_loop` / `_run_startup_recovery` (`core/crawler_manager.py`
  L502-600) — reclaims abandoned inflight claims and promotes due retries; this is about a
  **crashed worker's claim**, not about network connectivity.
- `docs/architecture/history/frontier-redis-failure-semantics.md` (Step 7, and its §11
  follow-up) — a thorough, tested design for what happens when **Redis** is down during
  scheduling or seed loading. Its own scope statement (§2, "Redis becomes unreachable (network
  blip, restart, timeout)") confirms this document is about the frontier's storage backend, not
  about the crawler's ability to reach **target websites**.
- `core/crawler_manager.py` header comment for `RedisMediaEvidenceStore` construction
  explicitly documents a *deliberate* choice **not** to silently degrade on a Redis outage for
  media evidence (L62-67) — again, Redis-outage handling, not target-network handling.

**Conclusion:** none of this existing, carefully-designed recovery machinery applies to the
incident's failure mode. The incident's failure mode (local Internet down, Redis itself
reachable throughout since it runs on `localhost` per `metadata.redis.host` in the benchmark
JSON) is a category this codebase has not yet built anything for.

## 8. Multi-Worker Implications

**VERIFIED FROM SOURCE:** grepping `core/redis_frontier.py` and `core/crawler_manager.py` for
any per-worker/per-host identity (`worker_id`, `host_id`, `hostname`, `socket.gethostname`,
`process_id`, `instance_id`) returns nothing. Claims are tracked purely by `url` + opaque
`token`; the frontier has no concept of which physical process or host owns a given claim, let
alone that host's network condition.

**ANALYSIS (not implemented, per scope):**

- A network-health signal derived **locally per process** (e.g. "have my last N fetches all
  failed with a connection-class error") is trivial to compute with what already exists in each
  crawler engine's fetch loop, and correctly isolates Host A's outage from Host B: Host B's own
  local failure counters are untouched by anything happening on Host A.
- A **globally-coordinated-through-Redis** health signal (e.g. a shared "network is down" flag
  all workers check) would incorrectly propagate Host A's local outage to Host B, causing B to
  pause or defer crawling it has no reason to defer — this directly violates the audit's stated
  scenario requirement ("A's outage must not cause B to stop crawling").
- However, a **fully local-only** signal has a blind spot the Redis frontier's own architecture
  makes relevant: retry/backoff and terminal-failure state (`ns:urls:failed_permanent`,
  `ns:retry_scheduled`) are global, shared across all workers/hosts pulling from the same
  namespace. If Host A (outage) is the one that happens to claim a URL for its 3rd and final
  attempt, that URL becomes globally `failed_permanent` — removing it from every other host's
  potential future work too, even though Host B was never impaired and could have succeeded.
  Nothing in the current retry/backoff design (§4/§5) is host-aware; the `attempt` counter and
  the terminal decision are pure per-URL state with no memory of *which* claim owner produced
  each failed attempt.

**RECOMMENDATION for Phase N2 (not decided/implemented here):** health *detection* should be
per-process (cheap, local, no false cross-host contamination), but its *effect* on the shared
Redis retry ladder needs care — the natural fix is for a process that has detected its own
outage to stop consuming the shared retry budget for URLs it claims while impaired (e.g. release
the claim without decrementing/consuming an attempt, or avoid claiming new work at all until its
local health check recovers), rather than trying to coordinate a global "network is down" state
through Redis.

## 9. Current Observability

**VERIFIED FROM SOURCE:** `RedisURLFrontier.get_status_counts()` /
`get_frontier_snapshot()`-equivalent methods (`core/redis_frontier.py` L680-770) expose only
aggregate `SCARD` counts of `visited` / `skipped` / `failed_permanent` plus ZSET sizes for
`inflight` / `retry_scheduled`. No breakdown by failure reason exists at this layer.

The `error` string passed to `mark_failed()` is used for exactly one thing:  a `logger.info`/
`logger.debug` line inside `RedisURLFrontier._complete()` (L582-588). It is never written into
Redis. Combined with `terminal_meta_ttl_seconds` defaulting to `0` (§4), by the time a URL is
`failed_permanent`, no queryable trace of *why* remains in Redis — only in process logs, if
those were retained and are being grepped by hand.

This matches the benchmark report's own generated statement (§1): the current reporting system
cannot distinguish target failures from network/infrastructure failures from extraction failures
from unknown failures — because none of that information survives past the log line. Building
that distinction would require, at minimum: (a) classifying the failure reason *before* it
reaches `mark_failed` (not decided in this audit), and (b) persisting the classification
somewhere queryable (Redis field, separate counter/set, or a structured log sink that is
subsequently mined) — a schema change to the current write-once terminal-state design.

## 10. Requirements for Future Design

(Restating the audit brief's list against what §2-§9 actually establish — not a design, just
scoped requirements a Phase N2 design must satisfy.)

- No permanent URL loss during local Internet outages: requires **both** a way to recognize
  "this failure looks infrastructure-shaped" **and** a way to avoid consuming the existing
  3-attempt retry ladder for it (§4, §6).
- No retry-budget consumption for infrastructure outages: today attempt count is
  failure-cause-blind (§4) — this is the central gap.
- Detection without excessive probe traffic: no probing exists today (§7); a design must define
  a cadence.
- False-positive resistance: current 3-attempt/15s ladder already conflates a real dead target
  with an outage — any classifier must not make that worse by misreading a genuinely dead site as
  "we're offline."
- Multiple independent health endpoints: not evaluable from source — no such concept exists yet.
- Short probe timeout: not evaluable from source.
- Recovery detection: not evaluable from source — nothing currently re-checks "are we back."
- Per-host isolation: §8 — must be local-detection, Redis-shared-state-aware-of-effect.
- Compatibility with the Redis frontier: the retry_scheduled/backoff primitive (§5) is reusable;
  the terminal `failed_permanent` set currently has no path back (§5) — a design must either add
  one or avoid ever terminally failing a URL for a reason later found to be infrastructure-caused.
- No crawl URL pollution: any health-check target must not itself be enqueued as/confused with a
  crawl URL — not evaluable further without designing the mechanism.
- Negligible normal-operation overhead: not evaluable from source; current per-fetch cost has no
  health-check overhead today because none exists.

## 11. Evaluation of Proposed Health-Check Approaches

Both are being evaluated only against the failure path traced in §2-§6, per scope — no
parameter values chosen.

**A. Check connectivity every N URLs**

- Advantage: fixed, predictable probe cadence; independent of failure rate, so it still catches
  an outage even in a crawl phase with a naturally low failure rate (e.g. mostly-successful
  crawling of healthy targets interspersed with one dead one).
- Disadvantage: during a **sustained** outage, "every N URLs" fires at whatever rate URLs are
  being (attempted and) failed — given §4's finding that a URL reaches terminal failure in
  roughly tens of seconds under continuous failure, N could be consumed and reached quickly
  regardless of intent, but the check is disconnected from *why* those N URLs failed — it could
  just as easily fire N URLs into a genuinely bad batch of dead target sites and produce a
  false-positive "outage" read if the check itself isn't robust (multiple independent
  endpoints, §10).
- Relative to the traced path: this approach needs a counter incremented somewhere in
  `HybridCrawler.worker()` or `_run_engine_plan()` — no such counter exists today (§9).

**B. Check connectivity after N consecutive failures**

- Advantage: directly targets the failure mode in §6 — an outage manifests as a burst of
  same-shaped failures, and this approach only spends probe traffic when there's already
  evidence something is wrong, so near-zero overhead during healthy operation (aligned with the
  "negligible normal-operation overhead" requirement).
- Disadvantage: "consecutive" needs a precise definition given this codebase's concurrency
  model — `HybridCrawler` runs `concurrency=25` (per this incident's config) workers
  concurrently across unrelated domains (§2 step 3-4); a global consecutive-failure counter
  shared across all 25 concurrent workers would trigger on 25 *simultaneous*, unrelated
  single-attempt failures just as easily as on a true outage, and would need to be
  disambiguated from "N different dead target sites happened to be claimed back to back" — the
  same false-positive risk as approach A, just measured differently. A per-worker consecutive
  counter avoids the cross-domain conflation but multiplies detection latency by `concurrency`
  in the worst case (each worker needs its own N failures before any of them probes).
- Relative to the traced path: this fits more naturally at the `mark_failed` call site
  (`hybrid_crawler.py` L342) or inside `_run_engine_plan`'s failure return, since that is
  already the single point every failure — regardless of engine — funnels through.

Neither approach is inherently superior from the source alone; B is better aligned with "detect
only when there's already a symptom" (lower overhead, directly tied to the observed failure
shape in §6), but needs careful scoping (per-worker vs. global counter) given this codebase's
concurrency model, which A does not need to solve since it isn't failure-triggered.

## 12. Evidence vs Inference

**VERIFIED FROM SOURCE:**
- The exact call chain from claim to `failed_permanent` (§2).
- That all non-HTTP-response fetch failures collapse into one generic, unclassified exception
  branch, for the `async` and `http` engines specifically (§3).
- That the frontier retry ladder (3 attempts, 5s/10s backoff) applies identically regardless of
  failure cause (§4).
- That `ns:urls:failed_permanent` is never read to requeue a URL, in the currently active
  Redis-frontier configuration (§5).
- That no connectivity/health-check/circuit-breaker mechanism exists anywhere in this codebase
  (§7).
- That no per-worker/per-host identity exists in the frontier (§8).
- That failure-reason strings do not survive past a log line (§9).

**OBSERVED FROM RUN DATA:**
- The incident run's actual configuration values (`max_retries=3`, `base_backoff=5.0`,
  `recovery_interval=30.0`, etc.) and outcome counts (§1).
- The report generator's own admission that failure subcategories are not tracked (§1, §9).

**INFERENCE:**
- That an internet outage of unknown timing occurred during the run (user-reported, not
  independently verifiable from any artifact inspected in this audit).
- That, *given* such an outage occurred and persisted, the traced mechanism (§2/§6) would
  convert affected URLs to `failed_permanent` within roughly tens of seconds to a couple of
  minutes each — this follows deductively from §4's backoff/recovery-interval arithmetic, not
  from directly observing the outage.

**Is the observed result (22,189/22,359 = 99.2% permanent failure) consistent with the current
architecture converting a local connectivity outage into permanent URL failures?**

**Yes — INFERENCE, not proof.** The architecture (§2-§6) is fully capable of producing this
result: with `concurrency=25` workers, a ~15-second-to-terminal-failure ladder per URL (§4), and
22,359 URLs, the entire discovered set could be driven to `failed_permanent` in well under the
8h51m run window purely mechanically, with no requirement that any of the failures were
target-caused. This is consistent with, but does not prove, the internet-outage explanation:
**an equally consistent alternative** is that a large fraction of the 22,359 discovered URLs
(mostly from seed files described in the benchmark metadata as piracy/torrent/streaming/darkweb
lists, which include stale/dead links by nature) were simply dead, blocked, or otherwise
genuinely unreachable regardless of the user's own connectivity, and only a smaller unknown
fraction is attributable to the outage. **This audit does not have — and no artifact inspected
provides — the per-URL failure-reason data (§9) needed to separate these two contributions.**
Determining the actual split is not possible from current evidence.

## 13. Recommended Next Phase

Not a design — a scoped punch list for Phase N2 based on the gaps this audit found:

1. Decide and specify a local network-health signal (detection trigger — approach A vs. B vs.
   hybrid, §11 — and its parameters) that does not require Redis coordination for detection
   itself (§8).
2. Specify how a detected local outage changes the `mark_failed` → retry-ladder interaction
   (§4/§5) for claims made *while impaired*, without altering the ladder's behavior for
   ordinary target failures.
3. Specify whether/how a URL already in `ns:urls:failed_permanent` from a suspected-outage
   window can be identified and requeued (§5's finding that no such path exists today), scoped
   to *this* incident's recovery, separate from #2's prevention-going-forward.
4. Specify minimal failure-reason classification and persistence needed to make future incidents
   analyzable (§9) — at minimum enough to distinguish "target responded with an error" from "no
   response/connection-level exception" before the reason string is discarded.
5. Re-run this audit's §12 correlation analysis once #4 exists, on a subsequent run, to actually
   measure the outage-vs-genuinely-dead-target split that could not be determined here.

## 14. Files Inspected

- `core/crawler_manager.py` (full)
- `core/frontier.py` (full)
- `core/redis_frontier.py` (`__init__`, Lua scripts: `claim_next`, `_complete_claim_script`,
  `_renew_claim_script`, `_reclaim_and_promote_script`; `_complete`, `mark_visited`,
  `mark_failed`, `mark_skipped`, `reclaim_and_promote`, `get_domain_scan_telemetry`,
  status/snapshot methods)
- `core/crawler_router.py` (full)
- `core/claim_heartbeat.py` (full)
- `crawler/hybrid_crawler.py` (full)
- `crawler/async_crawler.py` (full)
- `crawler/http_crawler.py` (`__init__`, `fetch()`)
- `config.yaml` (crawler/frontier/search sections)
- `benchmark/results/overnight_e2e_crawler.json` (full — the incident's own run report)
- `docs/architecture/history/frontier-redis-failure-semantics.md` (headings + relevant sections,
  to confirm its scope is Redis-backend outages, not target-network outages)

**Not fully inspected (noted per the audit's evidence-discipline requirement, not assumed):**
`crawler/playwright_crawler.py`, `crawler/selenium_crawler.py`, `crawler/scrapling_crawler.py`,
`crawler/tor_crawler.py` — these were identified via `HybridCrawler`'s engine plan (§2 step 4)
as also participating in the escalation chain and very likely share the same generic
`except Exception as e: last_error = str(e)` pattern seen in `async_crawler.py` and
`http_crawler.py` (consistent code style, same author conventions throughout the codebase), but
this was **not verified line-by-line** for each file and is therefore not asserted as VERIFIED
FROM SOURCE above. `core/url_frontier.py` (the SQLite frontier) was not inspected in detail since
this incident ran with `frontier.type: "redis"` (confirmed in `config.yaml` and the benchmark
JSON's `metadata.backend`).
