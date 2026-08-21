# Phase N6 — Scheduler Backpressure & Lease-Lifetime Reproduction/Design

Status: **design phase only — no production code, Lua script, retry semantic,
lease semantic, or config default changed. Implementation deferred to N7.**

---

## 1. Status

Complete. This phase reproduces Finding N5-1 from
`docs/architecture/redis-frontier-concurrency-audit.md` against a real Redis
frontier under a controlled, synthetic, bounded workload; measures its
magnitude and mechanism precisely (not just "inflight is large"); evaluates
four candidate backpressure architectures on identical workloads; and
selects one for N7 to implement. No production file was modified (verified
in §23).

## 2. Scope

In scope: reproducing and measuring the scheduler-claims-faster-than-workers-
process defect (N5-1) against `core/redis_frontier.py` (unmodified) via a new,
isolated harness script; comparing candidate local-queue/scheduler
disciplines; selecting one architecture for N7.

Out of scope (explicitly, per the phase brief): implementing the fix in
`crawler/*.py`/`core/*.py`; N5-2 (`rate_limit=0` test flake); N5-3 (stale
docstring); N5-4 (stale-completion counter, though this phase's harness
independently demonstrates why it would be valuable — see §9); any real
website crawling; any multi-hour run.

## 3. Problem statement

Per N5-1: every `crawler/*_crawler.py` backend's `scheduler()` claims URLs
from Redis as fast as the per-domain rate gate allows and pushes them onto
an unbounded, un-throttled local `asyncio.Queue`, with no relationship to
how many of the `concurrency` `worker()` tasks are actually free. A claim's
Redis lease clock starts at claim time (inside `claim_next`'s Lua script);
`run_with_heartbeat` — the only thing that renews it — starts only once a
worker actually dequeues the claim. If the scheduler outpaces the workers,
claims accumulate in the local queue with their lease already ticking down
unrenewed, and N5 hypothesized (from code tracing + N4 production-telemetry
reconciliation, not a live load test) that this drives real retry-budget
waste, premature `failed_permanent`, and a narrow silent-completion-loss
window. N6's job was to turn that into direct evidence and a measured
design decision.

## 4. Existing architecture (re-verified in this phase, not re-audited)

Re-read directly in this phase to confirm N5's description still matches the
code exactly (no drift since N5): `core/redis_frontier.py` (all six Lua
scripts), `core/claim_heartbeat.py` (`run_with_heartbeat`,
`resolve_heartbeat_interval` — renews at `lease_ttl/3`, clamped below
`lease_ttl/2`), `core/frontier_executor.py` (`AsyncFrontier`, offloads Redis
calls via `asyncio.to_thread`), `core/crawler_manager.py` (`_recovery_loop`
calls `reclaim_and_promote` every `recovery_interval`, both wired and
running), `crawler/hybrid_crawler.py` in full, and a targeted grep
confirming all 5 other `crawler/*_crawler.py` backends share the identical
`self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue()` (no `maxsize`),
`scheduler()`/`worker()` shape. **VERIFIED: matches N5's description
exactly, no material drift.** Config defaults confirmed in `core/config.py`:
`concurrency=25`, `lease_ttl=90.0`, `recovery_interval=30.0`,
`reclaim_batch_size=200`, `domain_scan_limit=250`, `rate_limit=1.0`,
`max_retries=3`.

## 5. Reproduction methodology

A new harness, `tests/benchmarks/scheduler_backpressure.py`, was added
(matching this directory's existing convention — see
`tests/benchmarks/README.md`, `common.py`; same idiom as
`heartbeat_endurance.py`/`crash_recovery.py`). It calls the **real,
unmodified** `RedisURLFrontier`, `AsyncFrontier`, and
`core.claim_heartbeat.run_with_heartbeat` against a real Redis instance, with
a synthetic scheduler/worker/recovery loop that mirrors
`crawler/hybrid_crawler.py`'s `scheduler()`/`worker()`/`_recovery_loop` shape
exactly, parameterized so the local queue can be unbounded (today) or
bounded/gated (candidates). Fetches are `asyncio.sleep(latency)` — no real
HTTP, no real websites, per the phase brief. The only "private" call used is
`RedisURLFrontier._complete()` directly (the same method `mark_visited`/
`mark_failed` already call) so the harness can observe the `'stale'` return
value those public wrappers otherwise discard — no frontier behavior is
added or changed by this.

Two modes:
- **`overload`** — full scheduler+worker+recovery simulation; the main
  reproduction/comparison tool. Per-token lifecycle is recorded
  (`claimed_at`, `dequeued_at`, `completed_at`, `completion_result`,
  `claim_lost`) and cross-referenced against later claims on the same URL to
  derive: pre-processing lease expirations (claim superseded before any
  worker ever dequeued it), reclaimed-during-processing (`ClaimLostError`
  while a worker held a still-current-at-dequeue-time claim), stale
  completions (`_complete()` returned `'stale'`), and eventual recovery.
  This directly answers the brief's A–F lifecycle categories (§6 of the
  brief) from measured per-token data, not aggregate inference.
- **`stale`** — a deterministic, single-URL, 7-step walkthrough of the exact
  scenario in the brief's §11 (claim → sits → lease expires → reclaimed →
  promoted → original "worker" finally completes with the old token →
  verify `'stale'` and that the newer claim's state is untouched), same
  idiom as `tests/benchmarks/crash_recovery.py`.

## 6. Environment

Real Redis at `localhost:6379`, **db 2** (this repo's existing benchmark
convention — see `tests/benchmarks/README.md` — chosen specifically because
it is distinct from both production, db 0, and the pytest Redis suite, db 1,
so a run here can never collide with either). Every run uses a fresh,
timestamp-unique namespace (`n6_backpressure_<epoch_ms>` /
`n6_stale_<epoch_ms>`). The harness hard-refuses `--redis-db 0`. Production
db 0 verified unchanged before and after this entire phase (§23). Python
venv at `env/`. No real network requests were made anywhere in this phase.

## 7. Baseline experiment configuration

Primary overload workload (`baseline_unbounded`): `concurrency=20`,
`urls=600`, `domains=50`, `worker_latency=1.5s` (±20% jitter),
`lease_ttl=3.0s`, `recovery_interval=0.75s`, `rate_limit=0.015s`,
`max_retries=3`, `base_backoff=0.4s`, `max_backoff=2.0s`,
`reclaim_batch_size=200`, `queue_maxsize=0` (unbounded — today's production
architecture), `real_failure_rate=0.0` (every claim a worker actually
finishes processing succeeds — so any `failed_permanent` in the results is
by construction not a genuine fetch failure).

This deliberately compresses production's absolute constants (`lease_ttl`
90s → 3s, `recovery_interval` 30s → 0.75s) while preserving the same
*relative* relationship N4 exhibited: claim capacity
(`domains/rate_limit` ≈ 3,333/s) vastly exceeds worker completion capacity
(`concurrency/worker_latency` ≈ 13.3/s), a ≈250× ratio, the same order of
magnitude as N4's real 632× inflight-vs-concurrency ratio. This is a
**SIMULATED** reproduction of the mechanism with compressed time constants,
not a literal replay of N4's absolute numbers — labeled as such throughout.

A **negative control** (`balanced_control`) uses the same `concurrency=20`
but `domains=20`, `rate_limit=1.5s` (chosen so max claim rate ≈ completion
rate, both ≈13.3/s), `urls=150`, same `lease_ttl=3.0s` — still an
**unbounded** queue, to isolate whether the queue *type* or the *rate
mismatch* is the actual defect.

## 8. Baseline results

All figures below are **MEASURED** (real Redis, real Lua scripts, real
`run_with_heartbeat`, this session).

| Run | Success | Failed-permanent | Pre-proc. expiry | Mid-proc. reclaim | Stale completions | Peak inflight | Completion rate |
|---|---|---|---|---|---|---|---|
| `baseline_unbounded` (lease=3.0s) | 44/600 (7.3%) | 556/600 (92.7%) | 1,076 | 592 | 0 | 597 (29.9× concurrency) | 0.50/s (vs. 13.3/s theoretical) |
| `balanced_control` (rates matched) | 150/150 (100%) | 0 | 0 | 0 | 0 | 33 (1.65× concurrency) | 11.5/s |
| `baseline_generous_lease` (lease=6.0s) | 81/600 (13.5%) | 519/600 (86.5%) | 0 | 0 | 1,557 | 597 (29.9×) | 0.65/s |

`baseline_unbounded`: 1,076 + 592 = 1,668 = `total_reclaimed` from
`reclaim_and_promote` exactly, across 117 recovery sweeps. Peak local queue
depth reached 1,492 (against 600 seeded URLs — re-claims re-enter the local
queue too). This is the direct, controlled reproduction N5 recommended: the
unbounded queue, under a claim-rate/completion-rate mismatch of the same
order N4 exhibited, drives the overwhelming majority of URLs to
`failed_permanent` with **zero real fetch failures**.

`balanced_control` is the critical negative control: **the same unbounded
`asyncio.Queue()` code path, under matched claim/completion rates, produces
zero disruption** — peak inflight only 1.65× concurrency, 100% success. This
proves the defect is the *rate mismatch* the scheduler never checks for, not
something inherently wrong with an unbounded Python queue in isolation.

`baseline_generous_lease` (identical overloaded workload, `lease_ttl` doubled
to 6.0s) reproduces the *same* underlying defect but with its failure mode
**completely different in character** — see §9.

## 9. Evidence of pre-processing lease expiry and the stale-completion window

**Category B/C (pre-processing / mid-processing lease expiry) — MEASURED,
`baseline_unbounded`:** 1,076 claims were superseded by a later claim on the
same URL *before any worker ever dequeued them at all* (`dequeued_at` never
set); 592 more were dequeued while still current, but lost their claim
mid-flight (`ClaimLostError` from `run_with_heartbeat`'s renewal check)
before completing. This is the harness's key differentiator from a
`ClaimLostError` count alone: a claim can be dequeued *after* it was already
dead (the local FIFO queue has no idea a claim died while it sat waiting),
so raw `ClaimLostError` counts would conflate "genuinely reclaimed while
actively being worked" with "was already dead on arrival, just discovered
late." Both are counted, correctly separated, in this dataset.

**Category E (stale completions) — MEASURED at scale, `baseline_generous_lease`:**
N5 predicted this as a "narrow" window (a fast fetch completing before its
first heartbeat check, on a claim that turns out to already be
reclaimed) — narrow in the sense of a tight per-claim timing race. This
phase shows that under sustained overload, that "narrow" window becomes the
**dominant** failure mode whenever `heartbeat_interval` (`lease_ttl/3`)
exceeds `worker_latency`: at `lease_ttl=6.0s`, `heartbeat_interval=2.0s` >
`worker_latency=1.5s`, so `run_with_heartbeat`'s coroutine *always* finishes
before the first renewal check ever fires — every one of the 1,557 reclaims
in that run was discovered **only** via a stale `_complete()` call, never via
`ClaimLostError` (`pre_processing_lease_expirations=0`,
`reclaimed_during_processing=0`, `stale_completions=1,557 == total_reclaimed`
exactly). This is a direct, at-scale confirmation of N5's predicted
mechanism, and also confirms N5-4's finding is exactly correct: this failure
mode is invisible in production's status counters today (`_complete()`
returns `'stale'`, logged at DEBUG only, no counter) — it would present as
"work vanished with no error," a real observability gap for N7 to consider
alongside the primary fix.

**Category F (permanent failure with zero real fetch attempt) — MEASURED,
`baseline_unbounded`:** `real_failure_rate=0.0` was set for this run
(every claim a worker actually completes succeeds), so any `failed_permanent`
outcome is *by construction* not a genuine target failure. `category_f_check`
confirms `failed_permanent_count=556`, and cross-references
`url_to_tokens`/per-token `dequeued_at` to show all 600 URLs *were* dequeued
by a worker at some point across their attempt history — but this does not
mean the successful path was taken; 556 of them exhausted `max_retries=3`
via repeated reclaim-driven attempt increments before any attempt actually
completed. This is not inferred from aggregate counters — it is derived
directly from the same per-token lifecycle data as the categories above.
This directly reproduces N5's §7 hypothesis about the N4 run's
`failed_permanent=1245` vs. `visited=47` imbalance, at controlled scale.

**Deterministic single-URL confirmation (`stale` mode) — VERIFIED (real
Redis, deterministic, not probabilistic):**

```
worker_a_claimed (token A, attempt 1)
  -> sits in local queue (no renewal, no processing) --- exactly N5-1's gap
lease_expired (waited 2.0s)
recovery_sweep_1_reclaim  reclaimed=1 requeued=0
recovery_sweep_2_promote  reclaimed=0 requeued=1
worker_b_claimed (token B, attempt 2)
worker_a_stale_completion_attempted -> result = "stale"
worker_b_completed -> result = "visited"
```

All three pass conditions held: `worker_a_completion_rejected_as_stale=true`,
`newer_claim_state_untouched_by_stale_completion=true` (`get_status_counts()`
identical before/after A's rejected completion — proves no corruption),
`url_eventually_correctly_processed_by_worker_b=true`. This directly and
deterministically confirms categories C, D, and E from the brief's §6 in one
run, independent of the probabilistic `overload` runs above.

## 10. Retry-budget impact

`baseline_unbounded`: of 1,668 total reclaim events, 92.7% of URLs (556/600)
exhausted their entire 3-attempt retry budget without a single genuine
fetch. `baseline_generous_lease`: 86.5% (519/600), via the stale-completion
path instead. `option_a_tight_lease` (§13): even a *correctly bounded*
queue (maxsize=concurrency) burns real retry budget — 78/600 (13%) — if
`lease_ttl` is set too tight relative to the bound's own worst-case drain
time (see §14). In every case, retry budget was consumed **purely by
queueing/reclaim timing, not by target behavior** — directly confirming N5's
Invariant D violation (§3 of the N5 audit) with measured, reproducible
numbers instead of an inferred N4 reconciliation.

## 11. Stale-completion reproduction

Covered in full in §9 (aggregate, 1,557 stale completions at scale) and §9's
deterministic walkthrough (single-URL, 100% reproducible). Both confirm: (a)
the stale completion is correctly rejected (frontier's token-CAS holds under
this stress, consistent with N5's Invariant B/C findings — this phase found
**no counterexample** to Redis-side claim-atomicity correctness, at any
scale tested), and (b) the newer claim's state is never corrupted by the
stale attempt. **This phase found no evidence contradicting N5's core
safety conclusion** — the defect is entirely in the crawler-side scheduling
discipline, never in Redis-side atomicity.

## 12. Candidate architectures

All four evaluated on the **identical** `baseline_unbounded` workload
(`concurrency=20`, `urls=600`, `domains=50`, `worker_latency=1.5s`,
`lease_ttl=3.0s`, `recovery_interval=0.75s`, `rate_limit=0.015s`,
`max_retries=3`) — only the local-queue/scheduler discipline changes:

- **Option A** — `asyncio.Queue(maxsize=concurrency)`; scheduler blocks on
  `queue.put()` when full.
- **Option B** — `asyncio.Queue(maxsize=2*concurrency)` (prefetch multiplier).
- **Option C** — scheduler acquires a `concurrency`-sized semaphore before
  each claim, held for the claim's **full lifetime** (claim → completion,
  not just claim → dequeue), released by the worker only after
  `mark_*`/abandonment. Local queue itself left unbounded (irrelevant — the
  semaphore is the actual gate).
- **Option D** — Option A + Option C combined (bounded queue *and*
  semaphore).

No candidate touches `core/redis_frontier.py`, any Lua script, retry
semantics, lease semantics, or `run_with_heartbeat` — every candidate is a
pure change to the crawler-side `scheduler()`/`queue` construction, matching
the brief's constraint (§15 of the brief) that the fix must stay
process-local and not introduce a global Redis semaphore.

## 13. Comparative benchmark results

All **MEASURED**, same workload, single run each (not averaged over repeats
— noted as a limitation in §20).

| Architecture | Success | Failed-perm. | Pre-proc. expiry | Mid-proc. reclaim | Stale | Peak inflight | Peak local queue | Completion rate | Elapsed | Redis EVALSHA calls |
|---|---|---|---|---|---|---|---|---|---|---|
| Baseline (unbounded) | 44/600 (7.3%) | 556 | 1,076 | 592 | 0 | 597 (29.9×) | 1,492 | 0.50/s | 87.7s | 4,441 |
| **Option A** (1×) | **600/600 (100%)** | **0** | **0** | **0** | **0** | 41 (2.05×) | 20 | **12.91/s** | **46.5s** | **1,899** |
| Option B (2×) | 507/600 (84.5%) | 93 | 0 | 584 | 0 | 61 (3.05×) | 40 | 7.41/s | 68.4s | 2,825 |
| Option C (semaphore) | 600/600 (100%) | 0 | 0 | 0 | 0 | 20 (1.0×) | 0 | 12.90/s | 46.5s | 1,883 |
| Option D (A+C) | 600/600 (100%) | 0 | 0 | 0 | 0 | 20 (1.0×) | 0 | 12.89/s | 46.5s | 1,883 |

**Option A and Option C are statistically indistinguishable** on this
workload (both eliminate the defect entirely, both reach ≈97% of the
theoretical max completion rate of 13.3/s, both use ≈57% fewer Redis round
trips than baseline). **Option D adds nothing measurable over Option C
alone** — the queue bound is redundant once the semaphore already caps
outstanding claims at `concurrency`. **Option B — a seemingly modest 2×
prefetch — reintroduces the defect**: 93/600 (15.5%) still reach
`failed_permanent` with zero real failures, and completion rate drops to
7.41/s (44% below Option A), because at `lease_ttl=3.0s`, 2×-concurrency
worth of queueing delay (≈3.0s worst case) plus `worker_latency` (1.5s) can
exceed the lease. This directly falsifies "any bound is safe" — see §14.

## 14. Lease TTL interaction (critical metric)

Two additional runs isolate this relationship directly:

- **`option_a_tight_lease`** — Option A's exact bound (`maxsize=20`), but
  `lease_ttl` cut from 3.0s to **1.0s** (same workload otherwise): the
  defect **reappears** — 533 mid-processing reclaims, 78/600 (13%)
  `failed_permanent`, `eventually_processed_after_reclaim=222` (many
  recovered on a later attempt, but real retry budget was still spent).
- **`baseline_generous_lease`** (§8/§9) shows the same lesson from the other
  direction: doubling `lease_ttl` alone (3.0s → 6.0s) on the *unbounded*
  queue does **not** fix anything — it just changes *how* the failure
  manifests (from detected `ClaimLostError`/pre-processing-expiry to
  invisible stale completions), while `failed_permanent` stays at 86.5%.

**Mechanistic model (INFERRED from measured data, consistent across every
run in §13/§14):** a bound of size `N` has a worst-case claim-to-dequeue
wait of approximately `N / completion_rate`, and the full claim-to-
completion time is approximately `N / completion_rate + worker_latency`.
`lease_ttl` must exceed this with margin for the bound to be safe:
  - Option A (`N=20`): `20/13.3 + 1.5 ≈ 3.0s` — matches the working
    `lease_ttl=3.0s` case almost exactly (worked cleanly); cutting
    `lease_ttl` to 1.0s (well under 3.0s) broke it, as predicted.
  - Option B (`N=40`): `40/13.3 + 1.5 ≈ 4.5s` — exceeds the same
    `lease_ttl=3.0s`, explaining why it failed at a setting Option A
    handled cleanly.

**This directly answers the brief's §14 question ("how much prefetch can
safely exist before lease expiry becomes possible"): prefetch depth is not
independently safe or unsafe — safety is a joint property of
`(queue_bound, completion_rate, worker_latency, lease_ttl)`, and a fix that
only bounds the queue without also ensuring `lease_ttl` has adequate margin
over the bound's own worst-case drain time can still reproduce the original
defect.** Increasing `lease_ttl` alone, without bounding the queue, was
explicitly evaluated (`baseline_generous_lease`) and found **not** to be a
viable substitute for backpressure — per the brief's own guidance (§14 of
the brief), it merely hides the queueing problem behind a different (harder
to observe) failure mode and increases genuine-crash recovery latency.

## 15. Selected architecture

**Option A: `asyncio.Queue(maxsize=concurrency)`** (queue bound = exactly
`concurrency`, no multiplier), retaining production's existing
`lease_ttl=90.0s` default (see §17 for why this default already has ample
margin).

## 16. Why this architecture

- **Correctness, measured:** eliminated 100% of the reproduced defect on
  the tested overload workload (0 pre-processing expiries, 0 mid-processing
  reclaims, 0 stale completions, 0 `failed_permanent` without a real
  fetch) — identical to Option C.
- **Performance, measured:** reached 12.91/s completion rate (97% of the
  13.3/s theoretical maximum given `concurrency=20`/`worker_latency=1.5s`),
  ≈26× the baseline's 0.50/s, using 57% fewer Redis Lua-script round trips
  than baseline (1,899 vs. 4,441 `EVALSHA` calls) — the correctness fix and
  the throughput/Redis-load win come together, not as a tradeoff.
- **Simpler than Option C for equivalent results:** Option C requires a new
  synchronization primitive (a semaphore held across the claim's *entire*
  lifetime, spanning multiple coroutines/await points) threaded through six
  near-identical crawler backends. Option A is a one-line constructor change
  (`asyncio.Queue()` → `asyncio.Queue(maxsize=self.concurrency)`) to
  existing code that already exists in every backend — `scheduler()`'s
  existing `await self.queue.put(claim)` call already blocks correctly with
  zero further changes to `worker()`, `run()`, or any completion path. Given
  measured-equivalent correctness and performance, the simpler
  implementation is preferred (this is a preference among
  measured-equivalent options, not a claim that Option A measured better
  than C).
- **Option D confirmed redundant** — adding Option C's semaphore on top of
  Option A's bound produced no measurable improvement over Option A alone
  on any metric in §13. Not recommended: it is strictly more code for zero
  measured benefit.
- **Option B (2×) is measurably worse, not just "unnecessary"** — it
  reintroduces real correctness loss (15.5% spurious `failed_permanent`)
  for a *lower* throughput than Option A (7.41/s vs. 12.91/s), because
  workers spend real time processing claims that are then discarded as
  reclaimed. A prefetch multiplier is not a free "smooths out worker
  latency variance" knob; at this lease/latency ratio it is strictly worse
  on both axes measured. N7 should not default to a multiplier > 1× without
  first re-running §14's lease-margin analysis for the production
  `lease_ttl` (90s) and `concurrency` (25) — see §17.

## 17. Distributed-system implications

Option A is a **pure, process-local change**: it modifies only
`self.queue = asyncio.Queue()` and adds no new Redis keys, Lua scripts,
cross-process coordination, or global semaphore — it does not touch
anything §15 of the phase brief's distributed-worker constraint list (atomic
Redis claiming, multiple independent processes, lease recovery, stale-worker
fencing, retry semantics, domain rate limiting). Each crawler process
independently bounds *its own* local queue to *its own* `concurrency`; N
independent processes each running Option A behave exactly as N independent
processes do today, just without any single process's local scheduler
racing arbitrarily far ahead of its own workers. **This claim is INFERRED
from the design (no new distributed-coordination surface is introduced) and
supported by, but not independently re-verified via, existing evidence**:
N5's audit and `tests/benchmarks/distributed_benchmark.py` already establish
that the Redis-side claim atomicity Option A depends on (Invariants A/B/C)
holds under real concurrent multi-process load — the candidate designs do
not touch that layer at all, so no new distributed test was run in this
phase (would be a reasonable N7/N8 verification step, not a requirement to
validate this specific architectural choice, which is orthogonal to
cross-process coordination by construction).

## 18. Lease/recovery implications

At production defaults (`lease_ttl=90s`, `concurrency=25`), Option A's
worst-case claim-to-dequeue wait is `25 / completion_rate`. Using the N4
run's own measured `completed_per_sec=0.674` (aggregate, not per-worker) as
a conservative real-world floor: `25 / 0.674 ≈ 37s` — well under the 90s
lease, consistent with this phase's mechanistic model (§14) predicting
Option A is safe whenever `concurrency/completion_rate + worker_latency <
lease_ttl`, and 90s was likely already generous even for N4's real,
slow-multi-engine-fetch workload. **This specific production-scale
prediction is INFERRED from the compressed-scale harness's mechanistic model
plus N4's real counters — not independently measured at full 90s-lease
scale in this phase** (a full-scale run would take the better part of the
90s lease's own duration merely to observe one reclaim cycle, judged not
worth the wall-clock cost for a phase whose job was to establish the
mechanism and select an architecture, not to re-derive production-scale
absolute numbers already available from N4). N7 should re-verify this
specific margin against current production `lease_ttl`/`concurrency`
defaults before shipping, ideally via `get_domain_scan_telemetry`-style
lightweight production telemetry rather than a new synthetic run.
Genuine-crash recovery latency is unaffected by Option A: a real crash still
recovers within `lease_ttl + recovery_interval` worst case, unchanged from
today, since Option A does not touch `lease_ttl`.

## 19. Performance implications

Measured (§13): Option A is not a throughput/correctness tradeoff on the
tested workload — it is a strict improvement on both axes simultaneously
versus baseline (26× completion rate, 57% fewer Redis round trips, while
also being the *only* change that eliminates the defect). Against Option C
specifically, Option A/C are measured-equivalent; Option A's simplicity
(§16) is the deciding factor, not a performance difference. `worker
utilization` was ≈97-99% in every configuration tested including baseline —
this metric alone is **misleading in isolation** for this defect: baseline's
99% utilization was spent overwhelmingly on claims that were reclaimed
before or during processing (measured directly in §8/§9), not on genuine
completed work. N7's own telemetry, if any utilization-style metric is kept
for production monitoring, should pair it with a completion-rate or
reclaim-rate signal (this reinforces the N5-4 recommendation for a
stale/reclaim counter — see §22).

## 20. Limitations

- Every `overload` run in §8/§13/§14 is a **single run**, not averaged over
  repeats — inherent variance (Python/asyncio scheduling jitter, Redis
  round-trip latency variance) was not separately quantified. The
  qualitative conclusions (Option A/C eliminate the defect; Option B/tight-
  lease reintroduce it) are large-margin results (0 vs. hundreds of
  disruption events) unlikely to be an artifact of single-run noise, but
  the *exact* completion-rate/EVALSHA-count figures should be read as
  point estimates, not tight confidence intervals.
- All experiments used **one crawler process** (single Python asyncio
  process against real Redis) — no multi-process reproduction was run in
  this phase (see §17's justification for why this is not required to
  validate a process-local design, but it does mean N6 provides no new
  *direct* evidence about cross-process interaction effects of Option A,
  e.g. whether many independent processes each running Option A could
  collectively still overwhelm a shared Redis instance's domain rate gates
  — a question orthogonal to the defect this phase investigated).
- Absolute time constants (`lease_ttl`, `recovery_interval`,
  `worker_latency`) were compressed relative to production defaults for
  bounded, deterministic test duration (§7); §18 extrapolates to production
  scale via a mechanistic model and N4's real counters, not a second
  full-scale measurement.
- `real_failure_rate=0.0` was used throughout the main comparison runs by
  design (to unambiguously attribute every `failed_permanent` to
  queueing/reclaim, not genuine failure) — this phase did not separately
  measure how Option A interacts with a workload that also has a realistic
  genuine-failure rate; no reason to expect interaction (the two mechanisms
  are orthogonal — a bounded queue changes *when* a claim is dequeued, not
  what happens once a genuine fetch fails), but this was not directly
  measured.
- The N5-2 test flake (`test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`)
  reproduced once during this phase's regression re-run (§21) — consistent
  with N5's own characterization of it as an intermittent, low-probability,
  `rate_limit=0`-only, production-irrelevant flake; not investigated further
  per the phase brief's explicit instruction not to spend time on N5-2.

## 21. Implementation requirements for N7

1. Change `self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue()` to
   `asyncio.Queue(maxsize=self.concurrency)` in all six
   `crawler/*_crawler.py` backends (identical one-line change, matching the
   shared boilerplate N5 §4 already identified).
2. No other code change is required — `scheduler()`'s existing
   `await self.queue.put(claim)` already blocks correctly on a full bounded
   queue with zero further changes.
3. Do **not** add a prefetch multiplier (Option B) without first re-running
   this phase's §14 lease-margin analysis against production's actual
   `lease_ttl=90s`/`concurrency=25`/observed `worker_latency` — this phase
   measured a multiplier as low as 2× reintroducing the defect at a
   *tighter-than-production* lease/latency ratio; production's much larger
   90s lease may have more room, but that specific margin should be
   confirmed, not assumed.
4. Re-verify §18's production-scale safety margin
   (`concurrency/completion_rate + worker_latency < lease_ttl`) against real
   or recent production telemetry before or shortly after shipping — this
   phase's number (37s vs. 90s lease, from N4) is a reasonable prior, not a
   guarantee for future runs with different domain/engine mixes.
5. Strongly recommended, not required for N7's core fix but directly
   motivated by this phase's evidence (§9, §19): implement N5-4 (a
   stale-completion / reclaim counter surfaced in `get_status_counts()` or
   equivalent), since §9 showed the stale-completion failure mode can
   become the *dominant* manifestation of this exact defect class under
   certain lease/latency ratios, and it is currently invisible above DEBUG
   log level.
6. No change to `core/redis_frontier.py`, any Lua script, `lease_ttl`
   default, retry semantics, or `run_with_heartbeat` is required or
   recommended by this phase's findings.

## 22. Explicit items NOT changed in N6

No file under `crawler/*.py`, `core/redis_frontier.py`, `core/frontier.py`,
`core/claim_heartbeat.py`, `core/crawler_manager.py`, or `config.yaml`/
`core/config.py` was modified. No Lua script was modified. No retry or
lease semantic was modified. N5-2 (rate_limit=0 flake), N5-3 (stale
docstring), and N5-4 (stale-completion counter) were not implemented in this
phase, per the brief — N5-4 is now additionally motivated by direct evidence
(§9, §21.5) as a strong N7-adjacent recommendation, not a requirement of
this phase.

## 23. Git / diff verification

```
$ git status --short
?? docs/architecture/redis-frontier-concurrency-audit.md   (pre-existing from N5, untracked before this session)
?? tests/benchmarks/scheduler_backpressure.py               (new, this phase)
?? docs/architecture/scheduler-backpressure-design.md        (this document)

$ git diff --stat
(empty — no tracked file modified)
```

**Production files changed: NO.** Only new files were added: the N6 harness
(`tests/benchmarks/scheduler_backpressure.py`) and this design document. No
existing file was edited.

Redis production verification: `redis-cli -n 0 dbsize` = **50,686** before
this phase's experiments and **50,686** after — unchanged (also matches
N5's own before/after count, confirming production has been untouched
across both phases). `redis-cli -n 1 dbsize` (pytest suite) = **0** before
and after — unaffected, since every experiment ran against db 2. db 2's
pre-existing 74 keys (unrelated prior benchmark artifacts) were restored to
exactly 74 after this phase's own `n6_backpressure_*`/`n6_stale_*`-namespaced
keys were cleaned up.

## 24. Tests run

| Command | Result | Duration | Notes |
|---|---|---|---|
| `pytest tests/redis_frontier_test.py tests/redis_startup_recovery_test.py tests/frontier_test.py tests/frontier_executor_test.py tests/crawler_manager_recovery_test.py -q` (pre-experiment baseline) | 53/53 passed | 7.48s | Clean run, no flake this time |
| Same command (post-experiment regression check) | 52/53 passed, 1 failed | 8.08s | The 1 failure is `TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool` — **exactly** N5-2, the already-characterized `rate_limit=0` clock-jitter flake (N5 audit §6/§13). Not a regression: db 1 (pytest suite) was never touched by any N6 experiment (all ran on db 2), and N6 modified zero production files. Pre-existing, intermittent, documented. |

---

## Executive Summary

N5 hypothesized (from code tracing + N4 telemetry reconciliation, not a live
test) that the unbounded local `asyncio.Queue` in every crawler backend lets
the Redis scheduler claim URLs far faster than workers can process them,
driving real retry-budget waste and premature `failed_permanent` outcomes.
N6 built a real-Redis, synthetic-workload reproduction harness
(`tests/benchmarks/scheduler_backpressure.py`) and confirmed this directly:
under a controlled overload workload with **zero genuine fetch failures
injected**, the unbounded-queue baseline drove **92.7% of URLs to
`failed_permanent`** purely through reclaim-driven retry-budget exhaustion,
while a **rate-matched negative control using the identical unbounded-queue
code path** showed **zero disruption** — proving the defect is the
uncontrolled rate mismatch, not the queue type itself. A second baseline run
with a longer lease showed the *same* defect manifesting as **1,557 silently
rejected "stale" completions** instead — a direct, at-scale confirmation of
N5's predicted (and previously only narrowly, hypothetically described)
silent-completion-loss window. Four candidate architectures were then
compared on the identical workload: bounding the local queue to exactly
`concurrency` (**Option A**) or gating claims on free worker slots via a
semaphore (**Option C**) both eliminated the defect completely (0 spurious
failures, 26× the baseline completion rate, 57% fewer Redis round trips);
a 2× prefetch multiplier (**Option B**) **reintroduced** the defect
(15.5% spurious failures); combining a bound with a semaphore (**Option D**)
added no measurable benefit over either alone. **N6 recommends Option A —
`asyncio.Queue(maxsize=concurrency)`, a one-line change already compatible
with every crawler backend's existing `scheduler()` code** — for N7 to
implement, along with a re-verification of the lease/concurrency safety
margin (§14/§18) against current production defaults before shipping. No
production code, Lua script, lease semantic, or retry semantic was changed
in this phase.

**N6 STOP — production implementation deferred to N7.**
