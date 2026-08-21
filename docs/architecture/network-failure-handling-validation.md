# Network Failure Handling Validation

## Status

VALIDATION COMPLETE, with a real-world run addendum (§19) added 2026-08-21. No production
code was touched by the addendum either — it documents evidence from an already-completed
crawl run, nothing more.

This is Phase N4. Its inputs are `docs/architecture/network-failure-handling-design.md`
(Phase N2, including its "N3 Implementation Results" section) and
`docs/architecture/network-failure-handling-audit.md` (Phase N1). No production code was
changed in this phase — validation confirmed the N3 implementation already satisfies the
primary guarantee.

## 1. Validation Objective

Prove two invariants against the actual N3 implementation, not just re-read the design:

1. A temporary local Internet/network outage must not consume a URL's normal retry budget
   or cause `failed_permanent` solely because the crawler host is offline.
2. A healthy crawler host must continue operating normally when another host/process is
   offline.

Method: read the N3 source directly (`core/network_health.py`, `core/failure_classifier.py`,
`core/redis_frontier.py`'s `mark_deferred` script, `crawler/hybrid_crawler.py`'s
`worker()`/`scheduler()`/`_run_engine_plan()`, `core/crawler_manager.py`'s wiring,
`core/config.py`'s `NetworkHealthConfig`), inspect the existing N3 test suite for genuine
(not superficial) assertions, run the full test suite, and add one bounded deterministic
script for the one invariant (Phase 8, sustained outage) that had no existing coverage.

## 2. Baseline Test Results — BASELINE

Command: `python -m pytest tests/ -q --ignore=tests/benchmarks` (via project venv `env/`).

**324 passed, 2 skipped, 0 failed** (75.6s). Clean on this run.

Known flaky tests, run individually to check reproducibility:

- `frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool` —
  failed 4/5 runs in isolation (100 concurrent `get_next_url()` calls, some return `None`
  under a domain-rate-gate timing race). Passed when run inside the full suite. Confirmed
  **pre-existing** — this test exercises `frontier_executor_test.py`/domain-gate timing only;
  it does not touch `network_health.py`, `failure_classifier.py`, or `mark_deferred`.
- `redis_frontier_test.py::TestMultiWorkerCoordination::test_get_next_url_no_duplicates` —
  passed 10/10 in isolation; failed once (`8 == 9`) when run as part of the narrower
  N3-file-only run in §3. Same signature the N3 implementation doc already recorded
  (~10% failure rate, `rate_limit=0` sub-microsecond timestamp comparison in
  `_claim_next_script`). `_claim_next_script`/`add_url` are unmodified by N3.

Both are **pre-existing, N3-unrelated** per the evidence above (different code path,
reproducible on unmodified logic, documented in the N3 implementation notes before N4 began).
Not fixed in this phase, per scope.

## 3. Health State Machine Results — VERIFIED

Ran `tests/network_health_test.py` (20 tests) directly against `core/network_health.py`'s
actual `HealthController`/`ConnectivityProber` — all pass. Confirmed by direct source
inspection, not just the tests:

| # | Property | Result |
|---|---|---|
| 1 | Startup is `HEALTHY` | VERIFIED — `HealthController.__init__` sets `self._state = NetworkHealthState.HEALTHY` unconditionally (`core/network_health.py:131`). |
| 2 | Isolated ambiguous failure does not immediately cause `OFFLINE` | VERIFIED — `record_ambiguous_failure` only increments a counter and returns unless `>= trigger_threshold`; a single call cannot skip straight to `SUSPECT`/`OFFLINE`. |
| 3 | Threshold transitions to `SUSPECT` | VERIFIED — `test_threshold_reached_enters_suspect_then_probe_succeeds_back_to_healthy`. |
| 4 | Successful probe returns to `HEALTHY` | VERIFIED — `_run_suspect_confirmation`'s first branch; `test_first_probe_fails_confirmation_probe_succeeds_stays_healthy` covers the debounced case too. |
| 5 | Failed confirmation transitions to `OFFLINE` | VERIFIED — `test_both_probe_rounds_fail_confirms_offline`: two failed rounds, debounced by `confirm_delay_seconds`, required (matches design §3). |
| 6 | `OFFLINE` prevents new claims | VERIFIED at the `HybridCrawler.scheduler()` call site (§6 below), not inside `HealthController` itself — by design, `HealthController` only exposes state; the pause is the caller's responsibility (design §7). |
| 7 | `OFFLINE` recovery probing occurs | VERIFIED — `_run_recovery_loop` re-probes every `recovery_probe_interval_seconds` while `state == OFFLINE`. |
| 8 | Recovery requires configured successful rounds | VERIFIED — `test_recovery_confirm_rounds_then_offline_to_healthy`; `test_recovery_resets_consecutive_success_count_on_intervening_failure` confirms a single intervening failure resets the counter (no partial-credit flapping back to `HEALTHY`). |
| 9 | `OFFLINE` → `HEALTHY` resumes claiming | VERIFIED indirectly — `scheduler()`'s pause condition re-reads `self.health.state` every loop iteration (no cached decision), so recovery is picked up on the very next iteration. |
| 10 | Repeated failure/recovery cycles do not corrupt state | VERIFIED — `test_disabled_controller_never_transitions` + the recovery-reset test above; also exercised directly in §10 below across 150 real claim/defer cycles with no corruption. |
| 11 | No busy-loop while `OFFLINE` | VERIFIED by source inspection — `_run_recovery_loop` starts each iteration with `await self._sleep(recovery_probe_interval_seconds)` before probing; `scheduler()`'s pause branch does `await asyncio.sleep(0.5)` before `continue`, reusing the existing idle-poll sleep (`crawler/hybrid_crawler.py:498-506`) — no path spins without an `await`. |

All probes use `httpx.MockTransport` (`ConnectivityProber(transport=...)`) — no real network
calls made in this phase's HealthController testing, per the N4 instruction not to depend on
physical connectivity.

## 4. Failure Classification Results — VERIFIED

Ran `tests/failure_classifier_test.py` (19 parametrized test classes covering all 8
categories) — all pass, against `core/failure_classifier.py`'s actual `classify_failure`.

- **The previously-fixed `ssl:default` bug stays fixed**: `TestCategoryTwo_TargetConnection`
  explicitly parametrizes `"Cannot connect to host example.com:443 ssl:default"` and asserts
  it classifies as `TARGET_CONNECTION` (ambiguous), not `TLS_FAILURE`
  (`tests/failure_classifier_test.py:36`). Source-level confirmation:
  `_TLS_SUBSTRINGS` deliberately excludes a bare `"ssl"`/`"tls"` token and requires a more
  specific signature (`"[ssl:"`, `"certificate"`, `"handshake"`, etc.) — see the comment at
  `core/failure_classifier.py:105-114`.
- **Normal target behaviors remain target-attributed** (regression requirement from N2 §17):
  `TestCategoryOne_HttpResponse` and the antibot-signature test class
  (`tests/failure_classifier_test.py:177+`) confirm HTTP 403/404/429/500, Cloudflare
  challenge strings, CAPTCHA strings, and "too many requests" all classify as
  `HTTP_RESPONSE` — never ambiguous, never able to trigger the health-eval counter.
- Target DNS failure (category 3) strings (`"Temporary failure in name resolution"`,
  `"getaddrinfo failed"`, etc.) classify as `TARGET_DNS`, ambiguous — correctly feeds the
  trigger counter without itself granting exemption (exemption requires a confirmed
  `HealthController.state == OFFLINE`, checked at a different layer — see §5).

No classifier changes were needed; the implementation matches the taxonomy in design §5
exactly, including the "TLS/engine failures never trigger health-eval" rule (§5's reasoning
that a TLS handshake proves local routing already worked) and "unknown stays conservative"
(falls back to `UNKNOWN`, still consumes budget).

## 5. Normal Target Failure Results — VERIFIED

`tests/hybrid_crawler_test.py::test_healthy_target_failure_still_uses_normal_mark_failed`
(read directly, not just executed): a target that always returns `"Connection refused"`
against a `HealthController` that never leaves `HEALTHY` (mocked prober disabled) —
`max_retries=1` — produces `failed_permanent == 1`, `retry_scheduled == 0`,
`health.state == HEALTHY` throughout. This confirms `mark_failed` is still the path used for
ordinary target failures and that N3 did not accidentally divert them to `mark_deferred`.

`tests/frontier_test.py` and `tests/redis_frontier_test.py`'s pre-existing (pre-N3) retry/
backoff/terminal-failure tests were unaffected — `_complete_claim_script` is byte-for-byte
unmodified by the N3 diff (confirmed by the N3 implementation notes and by the unchanged
line count/content read directly in this phase, `core/redis_frontier.py:266-325`).

## 6. Local Network Outage Results — SIMULATED

`tests/hybrid_crawler_test.py::test_offline_at_completion_uses_mark_deferred_not_mark_failed`
(read directly): claims a URL, force-sets a real `HealthController` instance's `_state` to
`OFFLINE` (simulating a probe-confirmed outage — no physical disconnect), runs one worker
pass. Result, asserted directly against a real in-memory frontier:

- `failed_permanent == 0`
- `retry_scheduled == 1` (requeued, not dropped)
- `frontier._attempts[url] == 0` — the claim-time increment was fully undone

This is a **simulated** validation (mocked/forced health state, not a physical network
outage) — see §16 for the explicit limitation this carries.

`tests/frontier_test.py::test_mark_deferred_leaves_retry_budget_unchanged_and_never_fails_permanent`
and the Redis-backed equivalent in §12 extend this to the frontier layer directly.

## 7. Active-Claim Outage Results — VERIFIED (source) / SIMULATED (behavior)

Design §6's requirement — judge by `HealthController.state` **at completion time**, not
claim time — is implemented exactly as specified:
`crawler/hybrid_crawler.py:419-426` reads `self.health.state` only in the completion branch,
after the fetch has already returned, never before or during the claim. The
`test_offline_at_completion_uses_mark_deferred_not_mark_failed` test (§6) directly
reproduces the race: claim happens while constructing the test (implicitly `HEALTHY`), state
is forced to `OFFLINE` *after* the claim and *before* completion — exactly the "claimed
while healthy, network drops mid-fetch" scenario design §6 requires, and the assertion
confirms `mark_deferred` (not `mark_failed`) is used.

## 8. Multi-Worker Isolation Results — SIMULATED (single-process, two independent controllers)

`tests/hybrid_crawler_test.py::test_scheduler_pauses_claiming_only_for_its_own_offline_controller`
(read directly): two independent `HybridCrawler` instances, each with its own `URLFrontier`
and its own `HealthController` — `health_a` forced `OFFLINE`, `health_b` left `HEALTHY`. Both
schedulers run concurrently for 0.3s. Result:

- Host A's queue stays empty — it never claims while its own controller is `OFFLINE`.
- Host B's queue is non-empty — claims proceeded normally, completely unaffected by A.
- Final states: `health_a == OFFLINE`, `health_b == HEALTHY`, independently.

This is explicitly a **simulated multi-host validation** (two controllers in one test
process, as the N4 brief anticipates and permits) — not two physical machines or processes.

**No global Redis health key**: confirmed by source inspection —
`core/network_health.py` contains zero references to Redis, and `core/redis_frontier.py`
contains zero references to `HealthController` state (the only match is a docstring comment
on `mark_deferred` explaining *why* it exists, not a runtime read/write of any shared key).
`HealthController` is constructed once per `CrawlerManager` (`core/crawler_manager.py:186`),
holding only in-memory state (`self._state`, `self._ambiguous_failures`).

## 9. Recovery Results — VERIFIED

`tests/network_health_test.py::test_recovery_confirm_rounds_then_offline_to_healthy` and
`test_recovery_resets_consecutive_success_count_on_intervening_failure` (both read directly):
confirmed `OFFLINE` requires `recovery_confirm_rounds` **consecutive** successful probes to
return to `HEALTHY`, and a single failure mid-recovery resets the consecutive-success count to
zero rather than carrying partial credit forward. `scheduler()`'s claim-pause check
re-evaluates `self.health.state` every loop iteration, so resumption is immediate once the
controller flips to `HEALTHY` (no separate "resume" step exists to get out of sync).

Deferred URLs becoming eligible again uses the **existing** `retry_scheduled` ZSET +
`reclaim_and_promote` mechanism unchanged — confirmed by reading `mark_deferred`'s Lua script
(`core/redis_frontier.py:327-362`): it `ZADD`s into `ns:retry_scheduled` exactly like the
existing retry path, just with `deferred_requeue_delay_seconds` instead of the exponential
value. No new promotion mechanism was introduced.

## 10. Long-Outage Stability Results — SIMULATED (this phase, new)

No existing test exercised a *sustained* multi-cycle outage prior to this phase. Added one
bounded, deterministic script (not committed to the permanent suite — this is validation
evidence, not new production test coverage) at
`n4_phase8_long_outage.py` (scratchpad), run once against a real Redis instance
(`localhost:6379/1`), simulating 150 claim → `mark_deferred` cycles across 5 URLs (30 cycles
each), with recovery promotion (`reclaim_and_promote`) between rounds:

```
total_cycles=150
redis_execute_command_calls=480          (≈3.2 Redis calls/cycle -- linear, no blowup)
failed_permanent=0
retry_scheduled=0
negative_attempt_seen=False
attempt_drift_seen=False                 (attempt counter returned to exactly 0 every cycle)
post_recovery_visited=1
post_recovery_failed_permanent=0
PHASE8_OK
```

Confirms, over 150 cycles against real Redis (not mocked):

- no URL reaches `failed_permanent` from repeated deferral alone
- the attempt counter never goes negative and never drifts (net-zero every single cycle, not
  just on average)
- Redis call volume per cycle is small and constant (one claim script + one mark_deferred
  script + bookkeeping — no hidden per-cycle growth, no busy loop)
- after the simulated outage ends, a normal successful completion (`mark_visited`) behaves
  exactly as if the URL had never been claimed before (`attempt == 1` on the first
  post-recovery claim) — the outage window left zero trace on the retry budget

This is a **deterministic simulation**, explicitly not a real prolonged (multi-hour) outage or
physical disconnect — consistent with the phase's instruction to avoid real long-duration
runs.

## 11. Redis Failure Separation — VERIFIED

`FrontierUnavailable` (raised only from `RedisURLFrontier` on an actual Redis error) and
`HealthController`'s state machine are structurally independent, confirmed by source
inspection (not a new test — the separation is architectural, not behavioral, so grepping for
coupling is the correct verification method here):

- `core/network_health.py` — zero references to Redis, `FrontierUnavailable`, or the
  frontier module at all. Its `ConnectivityProber` talks to `httpx` against operator-configured
  external endpoints only.
- `core/redis_frontier.py` — zero runtime references to `HealthController`/`network_health`
  state (the only match is a comment).
- `crawler/hybrid_crawler.py::worker()` catches `FrontierUnavailable` in its own `except`
  branch (`crawler/hybrid_crawler.py:474-479`), completely separate from the
  `offline_at_completion` / `mark_deferred` branch — a Redis outage is abandoned for
  lease-based reclaim (pre-existing behavior, unchanged), never routed through
  `mark_deferred`, and never causes `HealthController.record_ambiguous_failure()` to be
  called (that only happens in the `status == "failed"` branch, which is a *fetch* failure
  path, not a frontier-completion-error path).

So: Redis unavailable + Internet healthy cannot be misclassified as a local network outage
(no code path connects them), and Redis healthy + Internet unavailable correctly still drives
`HealthController` via the fetch-failure path, independent of Redis's own availability.

## 12. Retry-Budget Invariants — VERIFIED (against real Redis)

Read directly and executed: `tests/redis_frontier_test.py`'s
`TestMarkDeferred` class (5 tests, all against a real Redis instance, not mocked):

- **Normal target failure**: existing `_complete_claim_script` tests (pre-N3, unmodified)
  confirm `attempt` stays consumed after a target failure.
- **Network deferral**: `test_claim_then_mark_deferred_leaves_attempt_budget_net_zero` —
  claim → `mark_deferred`, repeated 5 times on the same URL, asserts `claim.attempt == 1`
  every single time (never climbs) and the Redis `attempts` key reads `"0"`/`None` after each
  deferral.
- **Fixed vs. exponential delay**: `test_mark_deferred_uses_fixed_delay_not_exponential_backoff`
  — with `base_backoff=100`, a deferred requeue lands at `~now+3s`
  (`deferred_requeue_delay_seconds=3`), nowhere near the 100s exponential value, proving the
  target-failure ladder is not used for deferrals.
- **Stale deferred claim**: `test_stale_claim_mark_deferred_does_not_corrupt_newer_claim` —
  old claim's lease force-expired and reclaimed by a new claim (`attempt` now 2, new token);
  the old claim's late `mark_deferred` call is rejected by the Lua script's CAS check
  (`current_token ~= token` → `'stale'`) **before** any mutation runs. Verified directly against
  Redis state afterward: `attempts == "2"` (untouched), claim token still the new claim's
  token, `inflight` entry still present, and the new claim can still complete normally
  (`mark_visited` succeeds, `visited == 1`, `failed_permanent == 0`).

This is the single most safety-critical property in the whole design (a race that could
otherwise let a stale worker silently corrupt a newer claim's state), and it is verified
against real Redis, not a mock.

## 13. Observability Results — VERIFIED (source)

`crawler/hybrid_crawler.py::_log_completion` (`crawler/hybrid_crawler.py:304-345`, read
directly) emits all 8 fields design §10 specifies in one structured log line: `url`,
`attempt`, `failure_category` (`category.name` or `"success"`), `consumed_retry_budget`
(derived from `final_outcome`), `network_health_state`, `host_identity`, `timestamp`,
`final_outcome`. Called from three sites in `worker()`: skipped, failed/deferred, and
success — confirmed successful completions log `category=None → "success"`, not mislabeled
as a network failure. This matches the deliberate deviation the N3 implementation notes
already documented (logging at the `worker()` call site instead of `_complete()`, since only
`worker()` has `failure_category`/`network_health_state` on hand) — not re-litigated here per
N4 scope (§16's "do not rewrite N2/N3 design except for a genuine defect").

## 14. Configuration Values — VERIFIED (current defaults, no changes made)

Read directly from `core/config.py`'s `NetworkHealthConfig` (lines 160-223) and
`core/network_health.py`'s `NetworkHealthConfig` dataclass (which mirrors it):

| Option | Current default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `trigger_threshold` | `10` | HEALTHY→SUSPECT sensitivity |
| `probe_timeout_seconds` | `5.0` | per-endpoint |
| `probe_endpoints` | `gstatic.com/generate_204`, `msftconnecttest.com/connecttest.txt`, `captive.apple.com/hotspot-detect.html` | 3 independent vendors, hostname-based |
| `confirm_delay_seconds` | `5.0` | SUSPECT→OFFLINE debounce |
| `recovery_probe_interval_seconds` | `15.0` | OFFLINE self-loop cadence |
| `recovery_confirm_rounds` | `2` | OFFLINE→HEALTHY |
| `deferred_requeue_delay_seconds` | `10.0` | fixed, non-exponential |

**Evaluation**: internally consistent with the design's stated reasoning (§11) — all three
probe endpoints are independent, high-uptime, hostname-based (DNS genuinely exercised, per
design §4's rejection of bare-TCP probing). No correctness problem was found in these values
during this phase's validation (§6-§10 above all pass with these exact defaults, including the
150-cycle stability run in §10). **No tuning change is proposed** — per Phase 12's explicit
instruction, defaults are left as-is absent a concrete correctness problem, and none was found.

## 15. Known Flaky / Pre-existing Tests

See §2. Both are confirmed pre-existing (reproducible on unmodified, N3-unrelated code paths)
and are not touched in this phase, per scope.

## 16. Remaining Limitations

- **No physical network-disconnect test was performed.** Every "outage" in this validation
  is either a mocked `ConnectivityProber` (via `httpx.MockTransport`) or a directly
  force-set `HealthController._state`. This proves the state machine and completion-routing
  logic are correct given a confirmed `OFFLINE` state, but does **not** independently confirm
  that a real physical outage (Wi-Fi down, cable unplugged, resolver actually failing) reaches
  `OFFLINE` through the real probe path (`ConnectivityProber.probe_round` against the real
  `httpx` default transport and the three real configured endpoints) within the expected
  timing. This is the one gap N4 was explicitly told not to close with a real disconnect test;
  it remains open.
- **Sub-detection-window outages remain an accepted limitation**, restated from design §9's
  caveat: an outage that starts and fully resolves inside a single fetch's timeout window,
  without accumulating `trigger_threshold` ambiguous failures, is indistinguishable from an
  ordinary transient target failure and will consume budget normally. This is a property of
  any threshold-based detector, not a defect.
- **Engine-level exception-type precision remains unfixed** (documented already in the N3
  implementation notes, not re-opened here): a bare, message-less `asyncio.TimeoutError`
  becomes `UNKNOWN` rather than `TIMEOUT` because the six crawler engines substitute a generic
  string before the classifier sees it. Still safely consumes budget (conservative default);
  just doesn't contribute to the ambiguous-failure counter. Fixing this touches the six engine
  files, out of N3/N4 scope.
- **Numeric config defaults remain provisional** (§14) — no real failure-rate telemetry from
  an actual outage exists yet to further tune `trigger_threshold` etc. This is unchanged from
  the N3 implementation notes; N4 found no evidence requiring a different value.
- This incident's existing 22,189 `failed_permanent` URLs remain unchanged, per N2 §9's
  explicit recommendation against automatic blind recovery — N4 did not touch this.

## 17. Overnight Readiness Decision

**CONDITIONALLY READY.**

Reasoning:

- The primary guarantee (§1) is verified end-to-end at the code level: state-machine
  correctness (§3), classification correctness including the specific previously-fixed bug
  (§4), unchanged normal-failure semantics (§5), correct claim-time-vs-completion-time
  judgment (§7), CAS-safe stale-claim handling under real Redis (§12), multi-host isolation
  with no shared Redis state (§8, §11), and 150-cycle sustained-outage stability with zero
  drift/corruption (§10).
- The condition: this is all **simulated/mocked** validation of the detection *trigger*
  (§16). The actual real-world reliability of `ConnectivityProber` correctly reaching
  `OFFLINE` during a genuine physical outage — real DNS/TCP/TLS failures against the three
  real configured endpoints, within the configured timeouts — has not been exercised in this
  phase, per explicit N4 instruction not to require a physical disconnect. Everything
  downstream of "state == OFFLINE gets confirmed" is solid; the one link not independently
  re-verified here is "does a real outage actually flip that state in practice."
- Recommendation: safe to run overnight with the understanding that if a real outage occurs,
  the crawler's behavior *given* correct detection is now well-verified (no permanent URL
  loss, no budget consumption, clean recovery) — but a first real-world observation (or a
  short manual physical-disconnect smoke test, e.g. unplug the network for 2-3 minutes while
  a crawl is running and observe the logs transition HEALTHY→SUSPECT→OFFLINE→HEALTHY) would
  close the one remaining gap and justify upgrading this to READY.

## 18. Recommended Next Step

A short, manual, one-time physical connectivity test (disconnect for a few minutes during a
low-stakes crawl, watch the `network_health[...]` log lines transition through all three
states, confirm zero `failed_permanent` growth during the window) would close the §16/§17 gap
directly. This is an operational action for the user, not a further code or test-suite change,
and is out of scope to automate within N4.

STOP AFTER N4 — no N5 work performed or proposed here.

---

## 19. Addendum: Real-World Run Evidence (2026-08-21)

### 19.0 What this is

Not a new phase, not a re-audit, and not a design change. This is a record of what an actual
production-shaped crawl run (`seeds+query` mode, real internet, real Tor, real target sites)
showed, added as evidence against the §17/§18 gap (no physical-outage/real-world observation
had been made before this). Source material: a partial terminal excerpt covering roughly the
tail of the run (log lines around "Processed (955)" through "Processed (1000)") and the
machine-generated report `benchmark/results/test_run_N4.json`. No code was read, changed, or
re-audited beyond what was needed to correctly interpret fields already used in §1-§18
(`_engine_counts`, `_pages_failed`, `url_database` gating, `inflight` semantics) — cited by
file:line below where a claim depends on it.

Run parameters (from the JSON report `metadata`/`timing`): backend `redis`
(`localhost:6379/0`, namespace `crawler`), 25 workers, engine `auto`, seeds
`piracy_sites.txt`/`torrent_sites.txt`/`streaming_sites.txt`/`darkweb_seeds.txt`, query
`"Blast Full Movie download"`, `max_pages=1000`, `rate_limit=0.3`. Start
`2026-08-21T04:37:23Z`, end `2026-08-21T05:09:21Z` — **1917.42s (≈32 minutes)** of continuous
real-world operation, process-wall-clock timed.

### 19.1 Observed facts (directly supported by the log excerpt and/or the JSON report)

1. **The run completed on its own terminating condition, not a crash**: `hybrid_crawler:456 -
   Reached max pages limit, stopping crawler`, followed by a clean shutdown sequence
   (`crawler_manager:658 - Crawler stopped`, Redis connection closed and reopened for the
   report snapshot, then closed again). `processed=1000` matches `max_pages=1000` exactly.

2. **All six fetch engines were actually exercised this run, not just configured** — per
   `hybrid_crawler:578`'s `engine_usage` line, which increments once per completed URL
   (success or failure) at `crawler/hybrid_crawler.py:450`, keyed by whichever engine reached
   *terminal* completion for that URL:

   | Engine | Completions this run (final engine) |
   |---|---|
   | async | 573 |
   | tor | 322 |
   | http | 52 |
   | playwright | 31 |
   | scrapling | 22 |
   | selenium | 0 (see §19.2) |

   Sum = 1000 = `processed`, confirming this is an exhaustive breakdown, not a sample.

3. **Tor was exercised at real scale**: 322/1000 completions (32.2%) this run terminated via
   the Tor engine — consistent with the darkweb seed file being in scope. This is direct
   evidence Tor fetches ran for real (not mocked) against `.onion` targets.

4. **Scrapling and Playwright were exercised and succeeded**, not just attempted: the excerpt
   shows explicit successful completions ending each chain at that engine, e.g.
   `completion url=https://www.zhihu.com/question/2052713001292191716 ... final_outcome=visited`
   via `chain=async -> scrapling` (and 6 more zhihu.com URLs the same way in the excerpt
   alone), and `completion url=https://hinative.com/explore/questions/newest?...
   final_outcome=visited` via `chain=async -> scrapling -> playwright` (and one more hinative
   URL the same way). These are individually-traceable successes, not just the aggregate
   count in point 2.

5. **Selenium failed 100% of the time it was invoked, in a specific and consistent way**, and
   the escalation chain handled that failure correctly. In the excerpt, Selenium was escalated
   to for 3 distinct URLs (`moviesda.com.in/cdn-cgi/l/email-protection`,
   `hinative.com/questions/802932/answers`, `moviesda.com.in/sitemap_index.xml`); every one of
   the 9 visible attempts (3 URLs × 3 retries) failed identically: `WebDriver error: Message:
   session not created from disconnected: unable to connect to renderer` — a local
   browser-driver/environment problem (the driver process itself won't start), not a
   site-side or network-side failure. In 2 of the 3 cases the crawler escalated past Selenium
   to `http`, which then completed normally against the real target and got a real HTTP 404
   (`failure_category=HTTP_RESPONSE`, `network_health_state=healthy`,
   `final_outcome=retry_scheduled`) — i.e., a broken Selenium backend did not block, hang, or
   get misclassified as a network-health event; it was treated as a per-engine failure and the
   chain moved on, exactly as designed. The third case
   (`hinative.com/questions/802932/answers`) had its claim reclaimed by another worker
   (`ClaimLostError`, `crawler/hybrid_crawler.py:468-473`) before this worker reached the
   `http` fallback, so no terminal engine/outcome for that specific URL is provable from this
   excerpt. **This is evidence Selenium is non-functional in the environment this run executed
   in — a local/environment fact, not a claim about `selenium_crawler.py`'s logic.** No
   production code was touched to investigate or fix this, per instruction.

6. **No `SUSPECT` or `OFFLINE` network-health state was logged anywhere in the excerpt.** Every
   one of the 37 completion log lines in the excerpt that carries a `network_health_state=`
   field (`hybrid_crawler:334`) reads `network_health_state=healthy` — including both of the
   real `HTTP_RESPONSE` (404) failures. See §19.3 for what this does and does not prove.

7. **A real (non-simulated) `HTTP_RESPONSE` failure consumed normal retry budget, not the
   network-deferral path** — observed twice directly:
   `moviesda.com.in/cdn-cgi/l/email-protection` and `moviesda.com.in/sitemap_index.xml`, both
   `failure_category=HTTP_RESPONSE`, `consumed_retry_budget=True`,
   `network_health_state=healthy`, `final_outcome=retry_scheduled`. This is the same invariant
   §5 verified with mocks/forced state; here it is the same behavior under real network
   conditions, for two real URLs, with no health-state involvement.

8. **`hybrid_crawler:469` (`ClaimLostError` / lease-reclaim path) fired very frequently** in
   the excerpt — dozens of occurrences, concentrated on `.onion` URLs. This is a distinct code
   path from the network-health/`mark_deferred` machinery this document validates (it's the
   pre-existing lease-expiry/crashed-worker-recovery mechanism, not part of the N1-N4 scope).
   Recorded here only because it was heavily exercised this run; not analyzed further, per
   instruction not to re-audit.

9. **`crawler_manager:659 - Database status counts: {}` is explained directly by source, not
   a guess**: `self.url_database` (the SQLite mirror) is only written to when
   `_sql_mode_mirror` is enabled, gated at `crawler/hybrid_crawler.py:430-439`. This run's
   frontier backend was Redis (`metadata.backend: "redis"` in the JSON report), so the SQLite
   mirror path was never exercised and an empty dict is the expected value, not an anomaly.

10. **The in-run `failed=95` counter (log line, `hybrid_crawler:578`) and the report's
    `failed_permanent` count for this run (`1245`, JSON `this_run.failed_permanent_unique`)
    measure different things and are not directly comparable**: `_pages_failed`
    (`crawler/hybrid_crawler.py:403`) increments once per *completion this worker process
    logged as `status=="failed"`* (i.e., one fetch attempt outcome — most of which just
    consume one of up to 3 retry attempts and go back to `retry_scheduled`, not straight to
    terminal failure). The JSON's `failed_permanent_unique` is a frontier-state delta — URLs
    whose Redis status flipped to the terminal `failed_permanent` state during this run,
    which can happen after retries accumulated across multiple attempts/workers, including
    ones that never logged a "failed" completion at all (e.g. abandoned via `ClaimLostError`,
    point 8). **Why the two numbers differ by this much (95 vs. 1245) is not established here
    — that would require tracing individual URLs' full attempt history, which was not done.**
    Recorded as an open question, not explained away.

### 19.2 Follow-up observation: 15,799 in-flight URLs at end of run

The end-of-run snapshot (`counts`/`this_run` in the JSON report) shows **15,799 URLs in the
`inflight` state** (claimed, lease not yet expired/reclaimed, not yet completed) at the moment
the process stopped — against only 47 `visited` and 1245 `failed_permanent` this run, out of
19,422 discovered. `inflight` is the Redis `ns:inflight` ZSET (`core/redis_frontier.py:251`,
populated at claim time, removed only by completion or by the periodic reclaim pass —
`core/redis_frontier.py:397-414`, `recovery_interval: 30.0s`, `reclaim_batch_size: 200` per
the run's own configuration block in the JSON report).

**What can be said from this data alone**: this is a real, large backlog of claimed-but-unresolved
URLs at shutdown — consistent with (a) the run being stopped abruptly by hitting `max_pages`
while many workers were mid-fetch, especially on the slow/high-latency Tor engine (322
completions in 32 minutes, and Tor fetches were seen retrying), and (b) the periodic reclaim
pass only processing up to 200 stale leases per 30s pass, which would take multiple passes to
work through a backlog this size even if every one of these claims is genuinely just stale.

**What cannot be established from this data alone**: whether this 15,799 figure is benign
(workers legitimately busy, or backlog that drains normally on the next run's startup
recovery pass — `startup_recovery_max_passes: 50`, `startup_recovery_max_duration: 30.0s` per
the same config block) versus a sign of claims being taken faster than they can ever be
resolved or reclaimed (a structural backlog that would keep growing run over run). Both are
consistent with the numbers shown here; distinguishing them needs either a second run's
startup-recovery log output or a direct `ZCARD ns:inflight` / lease-age inspection, neither of
which was done in this addendum.

**Recorded as a follow-up item**, not a finding: check the *next* run's startup recovery log
line (or query `ns:inflight` directly) to see whether this backlog drains, stays flat, or
grows further.

### 19.3 What this addendum does and does not prove, relative to §16/§17

- **Does prove**: all six fetch engines run for real against real, uncontrolled internet and
  Tor targets under real concurrent load (25 workers, 32 minutes), and the specific invariants
  already verified with mocks in §3-§14 (ordinary `HTTP_RESPONSE` failures stay on the normal
  retry path; a broken engine backend escalates rather than corrupting state; `healthy` state
  holds under sustained real load without spuriously flipping) held in this real run, for the
  URLs actually traceable in the excerpt.
- **Does not prove**: that the network-health state machine correctly detects and transitions
  through `SUSPECT`/`OFFLINE`/back to `HEALTHY` in the real world. No such transition appears
  anywhere in the available log excerpt — the state stayed `healthy` throughout every traceable
  completion. That most likely means no real connectivity outage occurred during this run's
  visible window (a plausible and unremarkable explanation, not a defect), but the excerpt is
  partial by the user's own description, so it cannot be ruled out that a transition occurred
  outside the pasted window either. **Either way, no evidence of a state transition exists in
  the material reviewed for this addendum, so none is claimed.** The §16/§17 gap (no confirmed
  observation of `ConnectivityProber` correctly reaching `OFFLINE` during a genuine outage) is
  unchanged by this addendum and remains open.

### 19.4 Overnight-readiness status after this addendum

**Unchanged: CONDITIONALLY READY** (§17's reasoning stands as written). This addendum adds
real-world confirmation of the *non-outage* steady-state paths (§19.1 points 2-7) but adds no
new evidence about the one specific gap §17 already named — real detection of an actual
`OFFLINE` transition. §18's recommended next step (a short manual physical-disconnect smoke
test) is still the only thing that would close it.
