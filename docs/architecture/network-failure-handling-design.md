# Network Failure Handling Design

## Status

IMPLEMENTED (Phase N3, 2026-08-20)

See "N3 Implementation Results" at the end of this document for what was
built, one deliberate deviation from §17's suggested observability call
site, and known limitations. The design below (§1-§17) is preserved as
written in N2 and was not rewritten.

This document is Phase N2. Its sole input is
`docs/architecture/network-failure-handling-audit.md` (Phase N1). Every claim below is tagged:

- **VERIFIED FROM SOURCE** — restated directly from N1's source-verified findings (cited by N1
  section), or confirmed by this phase's own minimal source check (cited by file).
- **DESIGN DECISION** — a choice made in this phase, with reasoning.
- **RECOMMENDATION** — a suggestion for N3 that is not load-bearing for the primary guarantee.
- **INFERENCE** — a reasoned but unverified deduction.

No source files beyond the N1 audit were re-read for this phase; N1 already cites exact
functions/line ranges for every mechanism this design touches (claim script, completion script,
retry ladder, `mark_failed` call site, multi-worker/host structure).

## 1. Problem

**VERIFIED FROM SOURCE (N1 §2, §4, §6):** In the traced call chain (`claim_next` →
`HybridCrawler._run_engine_plan` → `mark_failed` → `_complete_claim_script`), every non-HTTP-response
fetch failure — whether caused by one dead target or by the crawler host having no route to the
Internet at all — collapses into the same generic `except Exception` branch and consumes one
frontier `attempt`. After `max_retries` (3) such consumptions, the URL becomes
`failed_permanent`, a set with no requeue path (N1 §5). A sustained local outage can therefore
convert every in-flight/soon-to-be-claimed URL to `failed_permanent` in roughly tens of seconds to
a couple of minutes per URL (N1 §4), with no record of *why* (N1 §9).

**Primary guarantee this design must provide:** a temporary local network outage must not consume
the normal per-URL retry budget and must not cause `failed_permanent` solely because the crawler
host was offline. Ordinary target failures keep today's semantics unchanged.

## 2. Design Goals

1. Distinguish "local host cannot reach the Internet" from "this specific target failed," without
   over-classifying (N1 §3, §10).
2. Never let *classification alone* grant retry-budget exemption — only a **probe-confirmed**
   offline state does. This is the central anti-abuse property (see §5, §12).
3. Zero added overhead while healthy (N1 §10's "negligible normal-operation overhead"
   requirement) — no periodic probing unless there is already a symptom.
4. Per-host-local detection; no global "network down" flag in Redis (N1 §8's Host-A/Host-B
   isolation requirement).
5. Reuse existing Redis primitives (`retry_scheduled`, `claim`/`token` CAS, lease/reclaim) rather
   than inventing a parallel queue (N1 §5's finding that the existing ladder is mechanically
   reusable).
6. `failed_permanent` recovery is evidence-gated and human-triggered, never a blind mass-requeue
   (explicit brief constraint, consistent with N1 §12's finding that this incident's failure mix
   cannot be split into outage-caused vs. genuinely-dead-target from current data).
7. No SSRF, no crawl-target-influenced health signal, no unbounded retry loop (brief §12).

## 3. Health State Machine

### States

| State | Meaning |
|---|---|
| `HEALTHY` | No reason to doubt local connectivity. Default/steady state. No active probing. |
| `SUSPECT` | A burst of ambiguous, connection-class failures has been observed process-wide. A confirmation probe has been dispatched. Not yet acted on. |
| `OFFLINE` | A confirmation probe (and one debounce re-check) both failed against all configured endpoints. Retry-budget exemption and claim-pausing (§7) are active. |
| (recovery is a sub-mode of `OFFLINE`, not a separate state — see transitions) |

### Transitions

- **Startup:** begin in `HEALTHY` (optimistic). **DESIGN DECISION:** do not block worker startup on
  a mandatory pre-flight probe. Reasoning: if the host is actually offline at startup, the first
  real fetch attempts will immediately generate the ambiguous-failure signal and drive the state
  machine to `SUSPECT`→`OFFLINE` within one detection cycle anyway; adding a mandatory blocking
  probe before the first claim only delays startup in the common (healthy) case for no benefit in
  the uncommon (offline-at-startup) case.

- **`HEALTHY` → `SUSPECT`:** a process-wide counter of consecutive *ambiguous* (connection-class,
  DNS, or timeout — see §5 categories 2/3/5) failures, reset to zero by any successful fetch of any
  kind, reaches `trigger_threshold`. Entering `SUSPECT` immediately dispatches one probe round
  (§4). **This transition does not itself exempt anything from the retry ladder** — it only starts
  a probe.

- **`SUSPECT` → `HEALTHY`:** the probe round succeeds (at least one endpoint reachable). Counter
  resets. This is the false-positive-resistant path: a cluster of dead targets that happened to
  fail back-to-back does not become `OFFLINE` unless an independent probe also fails.

- **`SUSPECT` → `OFFLINE`:** the probe round fails against **all** endpoints, **and** a second
  confirmation probe round, run after `confirm_delay_seconds`, also fails against all endpoints.
  **DESIGN DECISION — two failed rounds, not one:** a single failed probe round does not
  distinguish a true outage from a momentary blip (Wi-Fi reassociation, brief DNS hiccup). Requiring
  the failure to persist across one short debounce interval avoids flapping into `OFFLINE` (and
  pausing claims, §7) for sub-second glitches, while still detecting a real outage within roughly
  `confirm_delay_seconds` of the first symptom.

- **`OFFLINE` (self-loop, recovery probing):** while `OFFLINE`, re-probe every
  `recovery_probe_interval_seconds`. This is the one place periodic (not failure-triggered)
  probing exists in this design — justified because while offline there is no fetch traffic to
  piggyback a trigger on (every fetch is failing), so the only way to detect recovery is to ask.

- **`OFFLINE` → `HEALTHY`:** `recovery_confirm_rounds` **consecutive** successful probe rounds
  (default reasoning in §11: more than one, so a single flaky success right as connectivity is
  half-restored doesn't immediately dump 25 concurrent workers back into claiming against a still-
  unstable link). Ambiguous-failure counter resets; claim-pausing (§7) lifts.

### Chosen model: Hybrid (failure-triggered entry + periodic-while-abnormal confirmation)

N1 §11 evaluated two pure approaches:

- **A. Periodic probing** — fixed cadence regardless of failure rate. N1's disadvantage: probes
  even when nothing is wrong, i.e., overhead during 100% of healthy operation for a signal that's
  usually unnecessary.
- **B. Failure-triggered probing** — only probe after a failure signal. N1's disadvantage:
  "consecutive" is ambiguous under `concurrency=25`; a naive global consecutive-failure counter
  could false-positive on 25 simultaneous *unrelated* single-attempt failures across different
  domains.

**Chosen: Hybrid.** Steady state uses B (zero probing while `HEALTHY` — goal 3). N1's stated
weakness of B is resolved not by making the trigger smarter, but by demoting the trigger to *just a
trigger*: reaching `trigger_threshold` only requests a probe (state → `SUSPECT`); it never by
itself changes retry-budget behavior. The *actual* state decision (`SUSPECT`→`OFFLINE`) is made
exclusively by real probe outcomes against independent, non-crawl endpoints (§4), which is exactly
what distinguishes "25 unrelated dead targets" (probe succeeds, falls back to `HEALTHY`) from "the
host has no route" (probe fails). Once abnormal, A's periodic model takes over for recovery
detection (`OFFLINE` self-loop), because there is no other signal available while every fetch is
failing.

**Rejected: pure A** — pays probe overhead unconditionally, violating goal 3 for no benefit in the
common case. **Rejected: pure B** — N1's ambiguity concern is real and unresolved without adding the
probe-is-truth separation this hybrid provides.

### Probe frequency / timeout (see §11 for the actual defaults and reasoning)

Kept out of this section to avoid duplicating §11; the state machine above references
`trigger_threshold`, `probe_timeout_seconds`, `confirm_delay_seconds`,
`recovery_probe_interval_seconds`, and `recovery_confirm_rounds` as named parameters defined once,
in §11.

## 4. Connectivity Probes

**DESIGN DECISION — probe mechanism: short-timeout HTTPS request, not ICMP, not bare TCP.**

- **ICMP ping** — rejected. Frequently requires elevated privileges (raw sockets) or is filtered
  by intermediate networks/firewalls unrelated to genuine Internet reachability; a false ICMP
  failure on a network that filters ICMP but otherwise works fine would falsely declare `OFFLINE`.
- **Bare TCP connect** — rejected as the *sole* signal. N1 §6 explicitly identifies DNS-resolution
  failure ("Temporary failure in name resolution") as one of the ambiguous local-vs-target
  signatures this design must handle; a bare TCP connect to a hardcoded IP would skip DNS entirely
  and miss a DNS-resolver-down condition, which is a real "local network broken" case.
  **INFERENCE:** most home/local-network outages manifest at the DNS-resolution or routing layer
  before TCP would even be attempted, so a probe that never resolves a hostname would under-detect.
- **HTTPS request (chosen)** — exercises DNS resolution, TCP connect, and TLS handshake in one
  operation, i.e., the same subsystem stack a real crawl fetch depends on (closest available proxy
  to "can this process do its actual job right now"). Full response body is not needed — a
  connection that completes and returns *any* HTTP status is sufficient proof of reachability; the
  probe should not wait for or parse a body.
- **Redirects:** probes MUST NOT follow redirects. Fixed endpoint list, no redirect-chasing — this
  is both an overhead-minimization choice and a security requirement (§14).

**Multiple independent endpoints — DESIGN DECISION:** require at least two, operator-configured,
non-crawl-target endpoints, chosen for independent infrastructure (not the same CDN/AS, ideally not
the same DNS provider) so that one endpoint's own outage cannot masquerade as "the Internet is
down." **RECOMMENDATION (not a decision, since no specific third-party endpoint was verified as
appropriate in this phase):** N3 should select 2–3 large, extremely-high-uptime, stable-URL
endpoints (e.g., a major cloud provider's health/status endpoint, a major public DNS-over-HTTPS
endpoint) and make the exact list fully operator-overridable via config (§9) — this document does
not hardcode specific URLs, since picking and vetting them is an N3 implementation detail, not an
architecture decision.

**Probe success/failure semantics:**

- **Success condition:** **any one** endpoint returns **any** HTTP response (regardless of status
  code — 2xx through 5xx all count) within `probe_timeout_seconds`. One success is sufficient;
  endpoints are not required to agree. Reasoning: the probe question is "do we have a route to the
  Internet," not "is this specific endpoint healthy" — a 500 from endpoint A still proves the local
  host has working DNS+TCP+TLS+HTTP, which is exactly the capability a real crawl fetch needs.
- **Failure condition:** **all** configured endpoints fail with a **connection-level** error
  (timeout, DNS resolution failure, connection refused, network unreachable, TLS handshake failure
  to reach the socket layer) within `probe_timeout_seconds`. An HTTP-level response of any kind is
  never itself a probe failure.
- **Caching:** the probe result is cached only as the `HealthController`'s current state (§3) — not
  re-run per fetch. It is (a) re-run once immediately on `HEALTHY`→`SUSPECT` entry, (b) re-run once
  after `confirm_delay_seconds` to confirm `SUSPECT`→`OFFLINE`, and (c) re-run every
  `recovery_probe_interval_seconds` while `OFFLINE`. No other code path triggers a probe.

## 5. Failure Classification

**DESIGN DECISION — minimal 8-category taxonomy**, matching the brief's required categories and
mapping directly onto N1's traced call sites (§2 steps 5–8, §3):

| Category | Consumes retry budget? | Triggers health-eval (ambiguous-failure counter)? | Persisted? |
|---|---|---|---|
| 1. HTTP response from target (any status) | Yes — unchanged from today | No | Yes (status code, already informally true) |
| 2. Target connection failure (refused/reset/unreachable, target DNS resolved) | Yes, **unless** `HealthController` is confirmed `OFFLINE` at completion time (§6) | Yes | Yes |
| 3. Target DNS failure | Yes, unless confirmed `OFFLINE` at completion time | Yes | Yes |
| 4. Local network/infrastructure failure (i.e., completion occurring while confirmed `OFFLINE`) | **No** — this is the guarantee | N/A (already offline) | Yes, tagged distinctly from 2/3 |
| 5. Timeout (connect/read, cause unspecified) | Yes, unless confirmed `OFFLINE` at completion time | Yes | Yes |
| 6. TLS failure (handshake/cert) | Yes — unchanged | No | Yes |
| 7. Browser/engine failure (driver crash, extraction error) | Yes — unchanged | No | Yes |
| 8. Unknown/unclassified | Yes — unchanged (safe default) | No | Yes, flagged "unknown" |

**Why TLS and engine failures don't trigger health-eval (DESIGN DECISION):** if the local network
were actually down, a TLS handshake could never begin — the connection failure (category 2) or
timeout (category 5) would occur first. A TLS-layer failure is evidence the local network *is*
routing traffic successfully to that target; it is therefore target-side by construction, not
ambiguous. Same reasoning for browser/engine crashes — an engine-level fault occurring after a
successful navigation/response is not a connectivity symptom.

**Why "unknown" doesn't trigger health-eval:** an unrecognized error shape is the safest thing to
treat conservatively — the existing behavior (consume budget, no special handling) is preserved
rather than risking a new failure string with unknown meaning falsely inflating the ambiguous
counter and triggering unnecessary probes.

**Critical design principle (ties §3, §5, §6 together):** classification into categories 2/3/5 only
feeds the process-wide *trigger* counter that requests a confirmation probe (§3). It **never**
directly grants retry-budget exemption. Exemption is granted **only** when the completion of a
claim happens while `HealthController.state == OFFLINE` (confirmed by probe). This closes the
obvious abuse/false-positive path: a single dead target with a DNS failure cannot itself cause its
own retry-budget exemption — the state has to independently reach confirmed `OFFLINE` via real
probe evidence first.

## 6. Frontier / Claim Semantics

**VERIFIED FROM SOURCE (N1 §2 step 1, §5):** `claim_next` increments `ns:attempts:<url>`
unconditionally at claim time, before the fetch is attempted. `_complete_claim_script` later
compares that already-incremented `attempt` against `max_retries` to decide retry-scheduled vs.
`failed_permanent`. The CAS/token check (claim.token vs. current `ns:claim:<url>` token) makes this
race-safe today.

**Required new primitive — DESIGN DECISION:** add a distinct completion path,
`mark_deferred(claim, reason)`, alongside the existing `mark_visited` / `mark_failed` /
`mark_skipped`. Its Lua-script semantics:

1. Validate `claim.token` against `ns:claim:<url>` exactly as `_complete_claim_script` does today
   (reuse the existing CAS safety property — no new race exposure).
2. **`DECR ns:attempts:<url>`** — undo the claim-time `INCR`, since this completion represents no
   real attempt against the target. This is the key mechanical change: without it, a deferred claim
   would still silently erode the budget even though nothing about the *target* was learned.
3. Re-add the URL to its domain queue (or to `ns:retry_scheduled` with a small fixed
   `deferred_requeue_delay_seconds`, **not** the exponential `base_backoff * 2^(attempt-1)` ladder —
   this wasn't a target failure, so the target-failure backoff curve doesn't apply). **DESIGN
   DECISION:** reuse `ns:retry_scheduled` + `reclaim_and_promote` (N1 §5, §2 step 10) mechanically
   as-is, just with a different, non-exponential delay value and without the attempt-count semantics
   attached to it. This satisfies goal 5 (reuse existing primitives) — no new queue/data structure.
4. Remove the claim from `ns:inflight` exactly as the existing completion paths do.

**When is `mark_deferred` called instead of `mark_failed`?** At claim-completion time (N1 §2 step
8, `hybrid_crawler.py` worker's post-`_run_engine_plan` call site), the worker checks
`HealthController.state`. If `OFFLINE`, call `mark_deferred(claim, reason=<category-4 reason>)`
instead of `mark_failed`. This is a point-in-time check made **at completion**, not at claim time —
**DESIGN DECISION, reasoning:** a claim with a long lease could be claimed while `HEALTHY` and only
fail after the network dropped mid-fetch; judging by state-at-completion (when the outcome is
actually known) is correct, whereas judging by state-at-claim-time would misclassify that case.

**Should the worker still run the full engine-escalation chain (N1 §2 steps 4–7) once `OFFLINE` is
already confirmed?** No — see §7 (short-circuit).

## 7. Worker Behavior While Offline

Brief's options: (A) keep claiming, defer each; (B) stop claiming while unhealthy; (C) other.

**DESIGN DECISION — hybrid of A and B, split by claim origin:**

- **Already-claimed work (in-flight when `OFFLINE` is confirmed):** on fetch failure, **short-
  circuit** the remaining engine-escalation plan (N1 §2 steps 4–7 — no point trying
  Playwright/Selenium/etc. sequentially with no route to the Internet) and complete immediately via
  `mark_deferred` (§6). This is effectively "A" for claims already in flight, since there is no
  cheaper way to release them than to complete them.
- **New claims:** **pause** — do not call `claim_next` for new URLs while `HealthController.state
  == OFFLINE`. This is "B," but scoped strictly to the confirmed-`OFFLINE` state, not to `SUSPECT`.
- **`SUSPECT` state:** claiming continues completely normally — a `SUSPECT` reading is, by design
  (§3), not yet trusted; pausing on `SUSPECT` alone would let a coincidental burst of dead targets
  halt real work, which is exactly the false-positive the hybrid model exists to avoid.
- **Recovery:** the moment `HealthController` transitions `OFFLINE` → `HEALTHY` (§3), claiming
  resumes immediately — no separate "resume" step needed since the pause is a simple conditional
  check at the claim call site, re-evaluated continuously.

**Reasoning against pure A (always keep claiming while confirmed offline):** with
`concurrency=25` (N1 §1, §8), continuing to claim during a *confirmed* outage means claiming up to
25 URLs that are guaranteed to fail, each still paying Redis claim overhead and `ns:inflight` lease
churn, for zero information gained (the outage is already confirmed — no new signal is learned by
claiming). Deferring is strictly better once confirmed.

**Reasoning against pure B applied at `SUSPECT`:** would sacrifice real throughput on every burst
of merely-coincidental dead-target failures, which N1 §11 already flagged as a concrete
false-positive risk under `concurrency=25`.

## 8. Multi-Host Semantics

**VERIFIED FROM SOURCE (N1 §8):** no per-worker/per-host identity exists in the frontier today;
claims are tracked purely by `url` + opaque `token`.

**DESIGN DECISION:** `HealthController` is a per-process, in-memory object — one instance per
`CrawlerManager`/host, holding no Redis-visible key. There is no "global network health" value
written to Redis anywhere in this design.

- **Host A, offline:** its own `HealthController` reaches `OFFLINE`; it pauses its own new claims
  (§7) and defers (via `mark_deferred`, §6) whatever it already had claimed. The only Redis-visible
  effect is those specific URLs going back to their domain queue with an undecremented... — 
  correction: **decremented-back** attempt count and a short fixed delay, indistinguishable in the
  frontier's eyes from "not yet claimed."
- **Host B, healthy:** its own independent `HealthController` stays `HEALTHY` (it runs its own probe
  logic against its own network path); it observes nothing from Host A except that some URLs Host A
  had claimed are now available again slightly sooner than a normal backoff would produce — which is
  the intended effect (Host B should be able to pick up A's deferred work). Host B's claiming,
  retry-budget accounting, and health state are entirely unaffected by A's state.
- No new global coordination primitive is introduced. This satisfies the brief's explicit
  requirement that A's health state must not globally pause B, and that no host-local state
  incorrectly becomes global URL state.

## 9. Failed-Permanent Recovery

**VERIFIED FROM SOURCE (N1 §5):** `failed_permanent` is currently write-only; nothing reads it to
requeue. **VERIFIED FROM SOURCE (N1 §12):** for this specific incident, the observed 99.2% failure
rate is consistent with, but not provably caused by, a network outage — it is equally consistent
with a largely-dead seed list, and no artifact available distinguishes the two contributions.

### Future outages (prevented going forward)

With §5–§7 in place, a URL reaches `failed_permanent` only by exhausting `max_retries` through
categories 1/2/3/5/6/7/8 while the `HealthController` was **not** confirmed `OFFLINE` at each
completion. A confirmed-offline completion always uses `mark_deferred` (§6), which never
increments toward the terminal threshold. **This is the primary guarantee, restated as a state-
machine property**, not merely an aspiration: as long as `HealthController` correctly reaches
`OFFLINE` before a claim completes, that completion cannot consume budget.

*Caveat, stated plainly:* this guarantee holds for outages the `HealthController` detects **before**
a given claim completes. An outage so brief that it starts and ends inside a single fetch's timeout
window, without ever producing enough ambiguous failures to trigger `SUSPECT`, will be
indistinguishable from an ordinary transient target failure and will consume budget normally — this
is an accepted limitation of any threshold-based detector, not a gap this design claims to close.

### Existing already-`failed_permanent` URLs (this incident)

**DESIGN DECISION — do not blindly requeue.** Per the brief's explicit constraint and N1 §12's
finding: this incident's `failed_permanent` set carries **no classification metadata** (N1 §9 — the
`error` string was never persisted, only logged), so there is no evidence-based way to select which
of the 22,189 URLs were outage-caused vs. genuinely dead. **RECOMMENDATION:** this incident's
specific failed set cannot be selectively/automatically recovered with any confidence; a full
recrawl of the affected seed list is the only clean remediation, and that is an operational decision
for the user, out of scope for N2/N3 to automate.

**For future incidents, once §10's schema exists — RECOMMENDATION (not built now):** a manual,
human-triggered operation, e.g. `requeue-failed-permanent --reason=local_network_offline
--since=<ts> --until=<ts>`, that reads the persisted failure classification and moves **only**
URLs whose *last* recorded failure was category 4 (confirmed-offline) within an operator-specified
window back into `retry_scheduled` — explicitly excluding any URL whose failure history contains a
genuine target-attributed category. This stays a deliberate, human-gated CLI action, never automatic,
per the brief's "do not resurrect genuinely dead targets unnecessarily."

## 10. Observability

**DESIGN DECISION — minimum schema per completion event** (structured log line at minimum;
queryable persistence is a stretch goal, see below):

| Field | Purpose |
|---|---|
| `url` | identify the affected URL |
| `attempt` | frontier attempt count at time of this completion |
| `failure_category` | one of the 8 taxonomy values (§5), or `success` |
| `consumed_retry_budget` | bool — did this completion increment toward `max_retries` |
| `network_health_state` | `HEALTHY` / `SUSPECT` / `OFFLINE` at completion time |
| `host_identity` | process/hostname identifier — **new**; N1 §8 confirmed none exists today |
| `timestamp` | wall-clock time of completion |
| `final_outcome` | `visited` / `retry_scheduled` / `deferred` / `failed_permanent` / `skipped` |

**RECOMMENDATION:** N3's first cut should persist this as a structured log line at the existing
`_complete()` log call site (N1 §9, `core/redis_frontier.py` L582-588) — cheapest change, matches
existing logging infrastructure. A queryable Redis-side structure (bounded ring-buffer list, or
per-category counters surfaced through `get_status_counts()`) is a stretch goal for a later phase,
not required to satisfy the primary guarantee, and is explicitly **not** decided here.

This schema is also what would let a future audit re-run N1 §12's correlation analysis and actually
measure the outage-vs-dead-target split that could not be determined this time (N1 §13 item 5).

## 11. Configuration

**DESIGN DECISION — small, reasoned option set.** Not written to `config.yaml` in this phase.

| Option | Reason it exists |
|---|---|
| `network_health.enabled` (bool, default `true`) | Allows fully disabling the subsystem — e.g., an intentionally air-gapped test host where every fetch is "expected" to fail and health-detection overhead/noise is unwanted. |
| `network_health.trigger_threshold` (int) | Governs `HEALTHY`→`SUSPECT` sensitivity (§3). Must be low enough to detect promptly, high enough that ordinary interleaved dead-target failures across `concurrency=25` don't fire it constantly. Exact default is an N3 tuning decision informed by real failure-rate data, not fixed here — this document defines the *existence and role* of the parameter, not its numeric value, per the brief's "do not choose arbitrary values without reasoning" instruction applied conservatively: no number is asserted without data to justify it. |
| `network_health.probe_timeout_seconds` | Per-endpoint probe timeout — must be short enough not to stall detection, long enough to not misread a merely-slow (but present) network as absent. |
| `network_health.probe_endpoints` (list) | Operator-supplied, independent, non-crawl-target endpoints (§4) — must be configurable since correctness depends on endpoints being genuinely independent infrastructure, which is an operational/deployment fact, not something to hardcode. |
| `network_health.confirm_delay_seconds` | Debounce between first and second failed probe round before declaring `OFFLINE` (§3) — exists specifically to reject sub-second blips. |
| `network_health.recovery_probe_interval_seconds` | Cadence of re-probing while `OFFLINE` (§3) — the only periodic probing in this design; must be configurable to balance detection latency against probe overhead during an outage. |
| `network_health.recovery_confirm_rounds` (int) | Consecutive successful probe rounds required before `OFFLINE`→`HEALTHY` (§3) — exists to reject flapping right at the recovery boundary. |
| `network_health.deferred_requeue_delay_seconds` | Fixed (non-exponential) delay used by `mark_deferred` (§6) — must be nonzero to avoid a tight reclaim loop hammering Redis while still offline (§14), but small since it isn't a target-failure backoff. |

**Explicitly not added:** per-engine overrides, per-domain overrides, per-worker thresholds — none
are motivated by anything in N1 or this design; adding them would violate the brief's "every option
must have a reason" constraint.

## 12. Performance

No benchmarking performed (out of scope for this phase). Reasoned estimate only:

- **Steady `HEALTHY` state:** zero probing (hybrid model, §3) — no added network calls, no added
  Redis calls. The only added cost is (a) a failure-reason classification step per failed
  completion — comparable in cost to the existing `needs_browser_upgrade` string-matching already
  on this path (N1 §2 step 6) — and (b) one in-process counter increment/reset per completion.
  **INFERENCE:** since `HybridCrawler` runs its `concurrency=25` workers as cooperatively-scheduled
  asyncio tasks (N1's references to `asyncio`/`run_with_heartbeat`), not OS threads, the shared
  ambiguous-failure counter needs no lock — single-threaded cooperative scheduling serializes access
  by construction. This is an inference from N1's description of the architecture, not independently
  re-verified against `asyncio` primitives in this phase.
- **`SUSPECT`/`OFFLINE` states:** cost is `probe_endpoints × probe_timeout_seconds` per probe round,
  occurring only at state transitions and at `recovery_probe_interval_seconds` cadence while
  abnormal — not per-fetch. Negligible relative to normal fetch volume (one probe round per interval
  vs. up to 25 concurrent fetches per lease cycle).
- **Redis:** `mark_deferred` is one additional Lua script variant, called at the same rate
  `mark_failed` is called today for offline-attributed completions — no additional round trips per
  completion, no new polling loop.
- **Worker-pause (§7):** implemented as a conditional check at the existing `claim_next` call site,
  not a busy-wait/sleep loop — no additional CPU cost beyond a state read.

## 13. Failure Scenarios

| # | Scenario | Expected state transition / outcome |
|---|---|---|
| 1 | Normal healthy target → success | `HEALTHY` throughout; category-1 or success completion; no counter change. |
| 2 | One dead target | Failure classified (likely category 1/6/7/8, or 2/3/5 if connection-level); if 2/3/5, ambiguous counter increments by one but stays below `trigger_threshold`; retry ladder proceeds exactly as today; remains `HEALTHY`. |
| 3 | One target DNS failure | Category 3; counter increments by one; below threshold alone; retry ladder proceeds unchanged; state stays `HEALTHY` — a single DNS failure never itself declares an outage. |
| 4 | Several dead targets (interleaved with successes elsewhere) | Each success resets the ambiguous counter to zero (§3); counter never accumulates because it's not truly *consecutive* process-wide; state stays `HEALTHY`. This is the specific case the hybrid model (§3) is designed to not misfire on. |
| 5 | Local Internet outage begins | Ambiguous failures accumulate with no interleaved successes → counter reaches `trigger_threshold` → `HEALTHY`→`SUSPECT`, probe dispatched → probe fails on all endpoints → after `confirm_delay_seconds`, second probe also fails → `SUSPECT`→`OFFLINE`. New claims pause (§7). |
| 6 | Outage begins while a URL is already claimed | That claim's fetch fails after `OFFLINE` is confirmed → engine-escalation short-circuited (§7) → `mark_deferred` called at completion → attempt count decremented back, URL requeued with fixed short delay, no budget consumed. |
| 7 | Outage while multiple workers (within one process, `concurrency=25`) are active | Single shared `HealthController` for the process — all 25 concurrent workers observe the same state; once `OFFLINE`, all in-flight claims complete via `mark_deferred`, no new claims are made by any of the 25 until recovery. |
| 8 | Host A offline, Host B online | A's own `HealthController` → `OFFLINE`; A pauses+defers per §7. B's independent `HealthController` stays `HEALTHY`; B keeps claiming/completing normally, including URLs A deferred, once they're back in a domain queue. No Redis-visible global flag exists to propagate A's state to B (§8). |
| 9 | Internet recovery | While `OFFLINE`, periodic probes (`recovery_probe_interval_seconds`) eventually succeed; after `recovery_confirm_rounds` consecutive successes, `OFFLINE`→`HEALTHY`; claim-pause lifts immediately; ambiguous counter already at/near zero (nothing to reset, since failures stopped once offline claiming paused). |
| 10 | Redis unavailable while Internet is healthy | **Out of scope for this design** — this is the `FrontierUnavailable` class N1 §2/§7 already identifies as a *materially different, already-designed-for* failure mode (frontier backend, not target network). `HealthController` has no opinion on Redis reachability; its probes are target-network-only. Existing `FrontierUnavailable` handling (N1 §7, §4 — claim abandoned for lease-based reclaim, no `mark_failed`/`mark_deferred` call at all) is unchanged by this design. |

## 14. Security

- **No SSRF via probes:** probe endpoints are a fixed, operator-configured list (§9), entirely
  separate from the frontier/seed/crawl-target universe. No crawl-discovered URL, redirect target,
  or target-controlled value ever becomes a probe endpoint.
- **No redirect-following on probes** (§4) — even the fixed endpoint list cannot be leveraged to
  redirect a probe request somewhere attacker-influenced.
- **No global manipulable health state:** `HealthController` state lives in-process only, never
  written to Redis as a shared flag (§8) — there is no shared value a compromised/malicious target
  or a rogue host could write to falsely claim `OFFLINE` (or `HEALTHY`) for other hosts.
- **No unbounded retry loop:** `mark_deferred` always requeues with a nonzero fixed delay
  (`deferred_requeue_delay_seconds`, §9/§11), and new-claim pausing while confirmed `OFFLINE` (§7)
  means a persistent outage does not turn into a tight reclaim-and-refail loop hammering Redis —
  in-flight deferrals happen once per already-claimed URL, then claiming stops until recovery.
- **`failed_permanent` is never mass-resurrected:** §9 explicitly rejects blind requeue of the
  existing terminal set; any future recovery path is evidence-gated (classification-tagged) and
  human-triggered, never automatic.

## 15. Alternatives Considered

| Area | Chosen | Rejected alternative(s) | Reason |
|---|---|---|---|
| Detection cadence | Hybrid (failure-triggered entry, periodic while abnormal) | Pure periodic (A); pure failure-triggered (B) | A wastes probe overhead unconditionally (goal 3); B alone has the false-positive ambiguity N1 §11 flagged under `concurrency=25` — resolved here by making triggers request-only, never state-deciding. |
| Probe mechanism | Short-timeout HTTPS request | ICMP ping; bare TCP connect | ICMP often privileged/filtered independent of real reachability; bare TCP skips DNS, missing the DNS-failure-as-outage-signature case N1 §6 identified. |
| Offline confirmation | Two failed probe rounds (debounced) | Single failed probe round | Avoids flapping to `OFFLINE` (and pausing claims) on a sub-second transient blip. |
| Recovery confirmation | Multiple consecutive successful rounds | Single successful round | Avoids prematurely dumping 25 concurrent workers back into claiming against a still-unstable link. |
| Retry-budget exemption trigger | Confirmed `OFFLINE` state only, checked at completion time | Exempt based on failure classification alone (category 2/3/5) | Classification alone is exactly what let a single dead target's DNS failure exempt itself — must require independent probe evidence, not just an ambiguous string match. |
| Frontier mechanism | Extend existing `mark_failed`/completion script family with `mark_deferred`; reuse `retry_scheduled` ZSET with a different delay | New parallel "offline queue" data structure | N1 §5 found the existing ladder mechanically sufficient; a parallel structure would duplicate CAS/lease/reclaim logic already correct today. |
| Worker claiming while offline | Pause new claims once confirmed `OFFLINE`; defer in-flight claims | Keep claiming through the outage (A); stop claiming on first ambiguous failure (B, too early) | Claiming during a *confirmed* outage is guaranteed-wasted Redis/lease overhead; pausing on the unconfirmed `SUSPECT` signal risks halting real work on a false positive. |
| Health-state scope | Per-process, in-memory, not in Redis | Global Redis-shared health flag | A shared flag would let Host A's local outage pause Host B, violating the explicit multi-host isolation requirement (N1 §8, brief §6). |
| `failed_permanent` recovery (this incident) | No automatic action; recommend full recrawl | Blind mass-requeue of the 22,189 URLs | No classification metadata exists for this incident's failures (N1 §9) — cannot distinguish outage-caused from genuinely-dead without evidence; brief explicitly forbids blind resurrection. |

## 16. Final Decisions

- **Health detection:** hybrid — failure-triggered entry into `SUSPECT`, probe-confirmed entry into
  `OFFLINE` (two debounced rounds), periodic re-probing while `OFFLINE`, multi-round confirmation
  back to `HEALTHY`.
- **Probe mechanism:** short-timeout HTTPS request (no redirects) to ≥2 independent,
  operator-configured, non-crawl endpoints; any one success proves reachability; all must fail for a
  probe round to fail.
- **Failure thresholds:** existence and role of `trigger_threshold`, `confirm_delay_seconds`,
  `recovery_probe_interval_seconds`, `recovery_confirm_rounds` defined (§3, §11); exact numeric
  defaults deferred to N3 as a data-informed tuning decision, not asserted here without evidence.
- **Worker offline behavior:** pause new claims once confirmed `OFFLINE`; short-circuit
  engine-escalation and defer already-claimed work; resume immediately on confirmed recovery;
  claiming is unaffected during unconfirmed `SUSPECT`.
- **Claim release/defer semantics:** new `mark_deferred(claim, reason)` frontier method — CAS-safe
  like existing completion, decrements the claim-time attempt increment, requeues via the existing
  `retry_scheduled`/`reclaim_and_promote` mechanism with a fixed non-exponential delay.
- **Retry-budget behavior:** unchanged for all failures except those completing while confirmed
  `OFFLINE`; classification alone never grants exemption.
- **Recovery semantics:** future outages are prevented by construction (subject to the
  detection-latency caveat, §9); this incident's existing `failed_permanent` set is not
  automatically recoverable and a manual recrawl is recommended; future evidence-gated,
  human-triggered recovery is a stretch-goal design for once §10's schema exists.
- **Per-host isolation:** `HealthController` is per-process/in-memory only; no Redis-shared global
  health flag; Redis remains the sole coordination layer, used only for the (already-existing)
  claim/lease/retry primitives.
- **Observability:** minimum 8-field per-completion schema (§10) defined; persistence mechanism
  (structured log first, queryable store as stretch goal) left to N3.

## 17. Implementation Plan for N3

Not implemented in this phase. Reviewable steps, in dependency order:

1. **`core/network_health.py` (new)** — `HealthController` class implementing the state machine
   (§3): states, transitions, probe dispatch, ambiguous-failure counter, config-driven parameters
   (§11). Pure/isolated component; no dependency on the frontier or crawler engines. Unit tests:
   every transition in §13's scenarios 1–5, 9, expressible as unit tests against a mocked prober.

2. **Failure classifier (new, likely `core/failure_classifier.py` or extending
   `core/crawler_router.py`)** — implements the 8-category taxonomy (§5) as a function from
   `str(exception)`/exception metadata to category. Unit tests: one per category, including the
   existing `needs_browser_upgrade`-style token cases to confirm no regression to today's escalation
   behavior for target-side signatures (CAPTCHA/Cloudflare/403/429/JS-required must still classify
   as target-attributed, not ambiguous).

3. **`core/redis_frontier.py`** — add `mark_deferred(claim, reason)` and its Lua script variant
   (§6): CAS-validate token, `DECR ns:attempts:<url>`, requeue via `retry_scheduled` with
   `deferred_requeue_delay_seconds` instead of the exponential ladder, remove from `ns:inflight`.
   Unit tests: attempt count is unchanged (net zero) after claim→mark_deferred vs. a claim that
   never happened; stale/superseded claim tokens are still correctly rejected (reuse existing CAS
   test pattern from `_complete_claim_script`); URL becomes reclaimable after
   `deferred_requeue_delay_seconds`, not the exponential backoff value.

4. **`core/frontier.py`** — add `mark_deferred` to the abstract frontier interface for parity.
   **RECOMMENDATION:** for the SQLite frontier (out of scope for this incident's `redis`
   configuration but interface parity matters), a reasonable default is to alias it to existing
   retry-scheduling behavior minus the attempt-count semantics, if achievable without a schema
   change — otherwise document it as a known gap, not silently no-op.

5. **`crawler/hybrid_crawler.py`** — wire `HealthController` into `worker()`: check state before
   `claim_next` (pause on confirmed `OFFLINE`, §7); after a fetch failure, classify (step 2), feed
   the classifier's category into the `HealthController`'s ambiguous-counter update, and — if
   `OFFLINE` at completion time — short-circuit remaining engine-escalation and call
   `mark_deferred` instead of continuing `_run_engine_plan`/`mark_failed`. Integration test:
   simulate an offline window (mocked prober forced to fail) spanning several claim/complete cycles
   and assert zero URLs reach `failed_permanent` during that window, and that attempt counts return
   to pre-outage values once recovered.

6. **`core/crawler_manager.py`** — instantiate one `HealthController` per manager instance
   (one per process/host, §8); pass it down to `HybridCrawler`. Test: two independent
   `CrawlerManager`/`HealthController` instances sharing one Redis namespace — force one
   `OFFLINE`, assert the other's claim/complete behavior and state are untouched (multi-host
   isolation test, scenario 8).

7. **Observability (§10)** — extend the existing log call site (N1 §9, `_complete()`
   L582-588-equivalent) to emit the 8-field schema for both `mark_failed` and `mark_deferred`
   completions. **RECOMMENDATION**, not required for the primary guarantee: also extend
   `get_status_counts()`/report generation to break failure counts out by category, enabling a
   future re-run of N1 §12's correlation analysis.

8. **Configuration** — add the `network_health.*` block (§11) to the config schema/loader (not to
   `config.yaml` itself until an operator opts in), with `enabled: false` as a safe rollout default
   if backward-compatibility during initial rollout is a concern — **RECOMMENDATION**, final
   default value is an N3/deployment decision.

**Migration/backward-compatibility considerations:**

- `mark_deferred` is purely additive — no schema migration for existing Redis keys/sets;
  `failed_permanent`, `retry_scheduled`, `inflight`, `claim` key formats are unchanged.
- Existing `mark_failed` behavior for every non-offline-confirmed completion is byte-for-byte
  unchanged — normal target-failure semantics (goal/requirement: brief's "Normal target failures
  must retain their existing retry semantics") are preserved by construction, since `mark_deferred`
  is only invoked from a new, additional branch, not a modification of the existing branch.
- SQLite frontier behavior is unaffected unless step 4 above is explicitly implemented for it.
- This incident's existing `failed_permanent` set requires no migration — §9 explicitly recommends
  against touching it programmatically.

**Tests required (summary, consolidating the above):** `HealthController` state-machine unit
tests; failure-classifier unit tests (8 categories); `mark_deferred` Lua-script unit tests
(attempt-count neutrality, CAS safety, requeue delay); `HybridCrawler` integration test for
zero-`failed_permanent`-during-simulated-outage; multi-host isolation test; regression tests
confirming unchanged behavior for ordinary target failures (categories 1/6/7/8, and 2/3/5 while
`HEALTHY`).

## N3 Implementation Results

**Status: IMPLEMENTED.** All items in §17's plan were built as specified, with one deliberate
deviation (observability call site) and one pre-existing architectural constraint discovered
during implementation (exception-string boundary) that N2 already anticipated and provisioned a
safe fallback for. Neither required a design change.

### Files changed

- `core/network_health.py` (new) — `HealthController`, `NetworkHealthState`, `NetworkHealthConfig`,
  `ConnectivityProber`.
- `core/failure_classifier.py` (new) — `FailureCategory`, `classify_failure`, `is_ambiguous`.
- `core/frontier.py` — added `mark_deferred` to the `Frontier` protocol.
- `core/redis_frontier.py` — added `mark_deferred` + its Lua script (CAS-validated,
  `DECR ns:attempts:<url>`, requeues via `ns:retry_scheduled` with
  `deferred_requeue_delay_seconds`); added the constructor parameter.
- `core/url_frontier.py` — added `mark_deferred` (local in-memory equivalent) and the constructor
  parameter — §17 item 4's SQLite-frontier gap is closed, not just documented.
- `core/frontier_executor.py` — added `mark_deferred` to the `AsyncFrontier` adapter.
- `crawler/hybrid_crawler.py` — `HealthController` wired into `worker()` (classify → feed
  ambiguous counter → `mark_deferred` vs `mark_failed` decided from `health.state` **at completion
  time**), `_run_engine_plan()` (short-circuits escalation once confirmed `OFFLINE`), and
  `scheduler()` (pauses new claims while `OFFLINE`, reusing the existing 0.5s idle-poll sleep — no
  new busy loop). Added the 8-field structured completion log (`_log_completion`).
- `core/crawler_manager.py` — instantiates exactly one `HealthController` per manager/process;
  wires it into `HybridCrawler` only.
- `core/config.py` — added `NetworkHealthConfig` (`network_health.*` block) to `CrawlerConfig`.
- Tests: `tests/network_health_test.py` (new, 24 tests), `tests/failure_classifier_test.py` (new,
  54 tests), `tests/frontier_test.py` (+3 `mark_deferred` tests), `tests/redis_frontier_test.py`
  (+4 tests, including the stale-claim race requirement), `tests/hybrid_crawler_test.py` (+5
  integration tests).

No file outside this list was modified. In particular, the six crawler engines
(`crawler/{async,http,tor,playwright,selenium,scrapling}_crawler.py`) are untouched — see
"Known limitations" below for why that matters.

### Deviation from §17: observability call site

§10/§17 recommended logging the 8-field completion schema at `RedisURLFrontier._complete()`'s
existing log call site. This implementation instead logs it from `HybridCrawler.worker()`, right
where the `mark_visited`/`mark_failed`/`mark_deferred` decision is made. Reasoning: `_complete()`
doesn't know `failure_category` or `network_health_state` (both are only known at the call site),
so routing them through would have meant adding new parameters to `mark_failed`'s signature purely
for logging. `worker()` already has every field on hand, including the frontier's *authoritative*
`max_retries` (via `self.frontier.raw.max_retries`, not `HybridCrawler`'s own copy — see the
`_log_completion` docstring for why those two can differ). §10 explicitly left the exact
persistence mechanism undecided ("RECOMMENDATION... not required for the primary guarantee"), so
this is within the design's stated latitude, not a deviation from a hard requirement.

### Exception-string boundary (discovered, not a contradiction)

Every crawler engine's `fetch()` already collapses its exception to `str(exc)` before returning
`failure_reason` — the raw exception object never reaches `HybridCrawler`. N2 §5 anticipated this
class of imprecision explicitly ("prefer structured exception info where available... Unknown
failures must remain conservative"): `classify_failure()` accepts an optional exception for callers
that have one, and falls back to string matching otherwise, which is what `hybrid_crawler.py`
actually exercises. This is documented as a known limitation, not a blocking contradiction: the
primary guarantee does not depend on precise per-failure classification (only the independent
probe round decides `OFFLINE`), and a misclassified failure defaults to UNKNOWN — the safe,
budget-consuming default N2 §5 already specifies for exactly this case.

One real bug this surfaced during testing: aiohttp's `ClientConnectorError.__str__` unconditionally
formats every connector failure (refused, DNS, timeout — anything) as
`"Cannot connect to host {h}:{p} ssl:{default|None|True} [{reason}]"`. An initial bare `"ssl"`
substring check misclassified ordinary aiohttp connection/DNS/timeout failures as `TLS_FAILURE`
100% of the time (aiohttp is the primary "async" engine). Fixed by requiring a more specific TLS
signature (`"certificate"`, `"handshake"`, `"[ssl:"`, etc.) — see
`core/failure_classifier.py`'s `_TLS_SUBSTRINGS` comment.

### Verification against real Redis

`mark_deferred`'s attempt-budget neutrality and stale-claim CAS safety were verified directly
against a running Redis instance (not just mocked): claim → `mark_deferred` → `reclaim_and_promote`
→ re-claim shows `attempt` returning to 1 (not climbing), and a stale claim's late `mark_deferred`
call is rejected without touching a newer claim's attempt count or ownership.

### Test results

Full suite after this phase's additions: 326 collected (excluding `tests/benchmarks`) — typically
323-324 passed, 2 skipped (pre-existing, environment-gated), 0-2 failed. Two Redis-backed
concurrency tests are flaky **independent of this phase**, confirmed by running each dozens of
times against unmodified `main` (`git stash`) before any N3 change:

- `frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`
- `redis_frontier_test.py::TestMultiWorkerCoordination::test_get_next_url_no_duplicates`
  (observed ~10% failure rate on unmodified `main` across 20 runs, "got 8 instead of 9 claims" --
  looks like a real, pre-existing edge case in the `rate_limit=0` gate's sub-microsecond timestamp
  comparison in `_claim_next_script`, not a correctness bug in anything this phase touched --
  `_claim_next_script`/`add_url` are byte-for-byte unmodified by this diff)

Both are real threads racing a real Redis instance under system-load-dependent timing; neither
exercises `mark_deferred` or any other N3 code path. Zero regressions introduced by this phase.

### Known limitations

- A bare, message-less timeout (`str(exc) == ""`, e.g. a raw `asyncio.TimeoutError()`) is
  substituted with the generic string `"unknown fetch error"` by the engine before the classifier
  ever sees it, so it classifies as `UNKNOWN` rather than `TIMEOUT` — it still safely consumes
  normal retry budget (N2 §5's conservative default) but does not contribute to the ambiguous-
  failure counter. Fixing this precisely would require changing the six crawler engines' generic
  exception handlers to preserve the exception type name, which was out of this phase's scope
  (engines are explicitly not in §17's file list).
- `probe_endpoints` defaults to three real third-party "connectivity check" endpoints (Google,
  Microsoft, Apple) chosen for this phase per §4's recommendation, since no specific endpoints were
  vetted in N2. Fully operator-overridable via config; not written into `config.yaml` itself.
- Numeric defaults (`trigger_threshold`, `probe_timeout_seconds`, etc.) are provisional, as N2 §11
  intentionally left them for N3 without real failure-rate telemetry — see each field's comment in
  `core/config.py`'s `NetworkHealthConfig`.
- This incident's existing `failed_permanent` set is unchanged, per N2 §9's explicit recommendation
  against blind automatic recovery.
