# Distributed Frontier Throughput-Ceiling Audit

Status: **investigation only — no production, Lua, or benchmark-harness code
changed.** This document follows on from `frontier-optimization-audit.md` and
`optimization_blacklist.md`. Those fixed a client-side bug (`URLUtils`'s
self-invalidating blacklist cache) that was the dominant cost in every prior
throughput number. This audit answers the next question: **now that that bug
is fixed, what is the real ceiling of the distributed (multi-process, Redis-
backed) frontier, and what causes it?**

Every number below is measured against this repo's actual code (no
hypothesized numbers). All measurement tooling is a temporary,
investigation-only script kept outside the repo (see "Tooling" below) — no
frontier, Lua, or benchmark-suite file was modified to produce this audit.

---

## 0. Starting point

Given baseline (`benchmark/results/distributed/postfix_dist_200k_w*.json`,
already committed): 200,000 URLs, 40 domains, rate_limit=0, retry_rate=0,
30s duration, workers ∈ {2, 4, 8, 16}, 0 duplicate claims, 0 failed attempts
in all runs.

| Workers | Throughput |
|--------:|-----------:|
| 2 | ~8,254 URLs/s |
| 4 | ~13,069 URLs/s |
| 8 | ~13,771 URLs/s |
| 16 | ~13,582 URLs/s |

Strong scaling 2→4, a plateau at 8, a small regression at 16. The question:
why.

---

## 1. Call-path map (Phase 1)

Read in full for this audit: `core/redis_frontier.py`,
`tests/benchmarks/distributed_benchmark.py`, `tests/benchmarks/common.py`,
`tests/benchmarks/frontier_benchmark.py`, `utils/url_utils.py`, plus the two
prior audit docs.

One successful URL, in the distributed benchmark (`_worker_main` in
`tests/benchmarks/distributed_benchmark.py:61-116`):

```
worker process (independent OS process, own Redis connection)
  → frontier.get_next_url()                          [core/redis_frontier.py:462]
      → self._claim_next_script(...)                 1 Redis round trip (Lua)
          Lua-internal work (server-side, still one round trip):
            ZRANGE domain_heads 0 K-1
            per candidate domain: ZRANGE queue 0 0 WITHSCORES, [rate check],
              ZREM queue, ZRANGE queue 0 0 WITHSCORES (new head),
              ZADD domain_heads, SET next_time, INCR attempts,
              HSET claim:{url}, ZADD inflight, HGET meta:{url} source_query
      → URLUtils.is_blacklisted(url)                  0 Redis calls — pure
                                                        client-side Python
                                                        (file stat + regex +
                                                        tldextract + set scan)
      ← FrontierClaim
  → (benchmark: no real fetch/parse — synthetic, "processing is free")
  → frontier.mark_visited(claim)                      [core/redis_frontier.py:596]
      → self._complete_claim_script(...)              1 Redis round trip (Lua)
          Lua-internal work: HGET claim:{url} token, ZREM inflight,
            DEL claim:{url}, SADD urls:visited, DEL attempts:{url},
            DEL meta:{url}
  → loop: next claim
```

Every state-changing frontier operation is exactly one Redis round trip
(one Lua script) — this was already confirmed correct and atomic by the
prior audit and is unchanged here. The two things **not** inside a Redis
round trip are (a) `is_blacklisted()`, called once per claim on the client,
and (b) the benchmark harness's own idle-poll/result-aggregation logic,
which happens outside the timed claim/complete path (result JSON is written
to disk only after each worker's loop exits).

Note: the production crawler backends (`crawler/*.py`) reach the frontier
through `AsyncFrontier`/`asyncio.to_thread` (`core/frontier_executor.py`),
which the distributed benchmark does **not** exercise (it calls
`RedisURLFrontier` methods directly from real OS processes, by design — see
the file's own docstring). This audit is scoped to the frontier's own
ceiling, matching the benchmark's scope; the `asyncio.to_thread` execution
boundary was already covered by Step 4 and is out of scope here.

---

## 2. Tooling (Phase 2 setup)

The Step 6 benchmark scripts (`distributed_benchmark.py`,
`frontier_benchmark.py`) don't capture per-operation latency across real OS
processes, and don't compute Redis CPU utilization correctly (only
cumulative `used_cpu_sys`/`used_cpu_user` snapshots via `ResourceMonitor`,
which — per this audit's own instructions — must **not** be read as
instantaneous). To measure Phase 2-4 properly, a temporary, throwaway probe
script was written (kept outside the repo, in the investigating session's
scratchpad — **not committed, not part of the shipped benchmark suite**):

- Reuses `tests/benchmarks/common.py` and `RedisURLFrontier` exactly as-is
  (same `build_frontier`, same Lua scripts, same synthetic-URL generator) —
  no frontier/Lua/keyspace change of any kind.
- Adds a `mode` switch (`claim_only` / `claim_visit` / `claim_delay`) to
  separate the claim path from the completion path (Phase 3).
- Captures per-operation latency (`time.perf_counter()` around
  `get_next_url()` and `mark_visited()`) inside each worker *process*, not
  just threads — matching the real distributed benchmark's process model.
- Computes **delta-based** Redis CPU utilization: snapshots `INFO cpu`
  before and after the run and divides `Δ(used_cpu_sys + used_cpu_user)` by
  wall-clock elapsed time — the correct way to turn that cumulative counter
  into a utilization percentage, as opposed to averaging raw snapshots.
- Snapshots `INFO commandstats` before/after to get exact per-command call
  counts and `usec_per_call` (Redis's own server-side timing) for the run.
- Samples `INFO stats`'s `instantaneous_ops_per_sec` periodically — this
  field *is* already an EWMA instantaneous rate inside Redis, safe to
  sample/average directly (unlike the CPU counters).
- Samples `INFO persistence`'s `rdb_bgsave_in_progress` throughout, to catch
  a confounding background save fork if one occurs mid-run.
- Reuses `common.ResourceMonitor` unchanged for client-side (worker process)
  CPU%, via `psutil`.

All experiments ran against namespaces isolated under Redis db 2 (never
production db 0, never the committed pytest suite's namespace), and every
namespace was cleared (`frontier.clear()`) at the end of its run. Nothing in
`datasets/domain_blacklist.txt` or the production Redis DB was touched.

---

## 3. Establishing the ceiling scientifically (Phase 2-4)

### 3.1 Main worker sweep

`claim_visit` mode (the real production shape: claim → mark_visited →
next), 150,000 URLs, 15s window, 40 domains, rate_limit=0 — same
configuration shape as the baseline, smaller scale for faster iteration.
Reproducibility against the full 200k/30s baseline scale is checked
separately in §3.4.

| W | claims/s | claim p50 (µs) | claim p95 (µs) | visit p50 (µs) | visit p95 (µs) | client CPU avg (of 1200%, 12 cores) | **Redis CPU (delta-based, of 100%)** | `evalsha` calls/s | `evalsha` µs/call (server-side) |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | 4,346 | 155.6 | 202.9 | 59.5 | 87.2 | 79.8% | 24.5% | 8,692 | 19.3 |
| 2 | 7,971 | 166.1 | 251.0 | 63.0 | 97.8 | 150.8% | 46.0% | — | — |
| 4 | 12,397 | 201.0 | 325.1 | 93.3 | 164.5 | 269.4% | 74.5% | 24,794 | 22.0 |
| 8 | 13,257 | 384.4 | 530.5 | 209.8 | 342.9 | 384.0% | **94.6%** | 26,514 | 26.9 |
| 16 | 13,446 | 690.6 | 957.7 | 462.9 | 758.1 | 449.1% | **96.1%** | 26,893 | 27.2 |

Reading this table:

- **Redis CPU rises in lockstep with worker count** (24.5% → 46% → 74.5% →
  94.6% → 96.1%) and **flattens exactly where throughput flattens** (8
  workers). This is the signature of a single-threaded server approaching
  saturation.
- **Client CPU never comes close to saturating** the host. At w=16, client
  processes use 449% out of a 1200% (12-core) budget — plenty of headroom.
  If client CPU were the ceiling, this number would be pinned near 1200%;
  it isn't.
- **Per-call Lua execution time (`evalsha` µs/call) rises only modestly**
  (19.3µs → 27.2µs, ~1.4x) while **client-observed claim latency rises much
  more** (155.6µs → 690.6µs, ~4.4x). The gap between "the actual work barely
  got more expensive" and "the client waited much longer" is queueing delay
  at Redis's single execution thread — not more expensive Lua, not slower
  networking, not slower Python.

### 3.2 Correcting the prior audit's hypothesis

`frontier-optimization-audit.md` (§2, evidence table) attributed the
8→16-worker plateau to "the benchmark host's own 12 logical CPU cores being
oversubscribed" by client processes, based on `children_cpu_percent` rising
to ~1100% (out of ~1200%) at 16 workers. Two things have changed since:

1. That measurement predates the blacklist cache fix
   (`optimization_blacklist.md`). Back then, every `is_blacklisted()` call
   cost ~5.7ms (a full file re-parse) — client CPU genuinely was the
   dominant, saturating cost at the time. This audit measures `is_blacklisted()`
   post-fix at ~64µs/call (§5) — an ~89x reduction. Client CPU dropped
   accordingly, and a different, previously-masked bottleneck (Redis's own
   CPU) is now the one that binds.
2. That measurement read `used_cpu_sys`/`used_cpu_user` as if they were
   already a percentage, without computing a time-normalized delta — exactly
   the mistake this audit's brief warned against. Read correctly (§3.1),
   Redis's own CPU is the one that's actually saturated at 8-16 workers, not
   the client fleet.

**Nothing in this audit contradicts the parts of the prior audit that
weren't about throughput** — the blacklist-cache fix itself remains correct
and is the reason this cleaner signal is visible at all.

### 3.3 Phase 3 — separating claim from completion

Two controlled experiments, using the `mode` switch described in §2:

**A. `claim_only`** (claim → never complete → next claim; this is a valid,
uncontaminated probe of the claim path alone, since `claim_next`'s Lua
already removes the URL from its domain queue regardless of whether it's
ever completed):

| W | claims/s | Redis CPU |
|--:|--:|--:|
| 1 | 6,082 | 23.7% |
| 16 | 18,236 | **99.6%** |

Removing the completion round trip entirely still drives Redis to 99.6% CPU
at 16 workers — proving the ceiling is about total Lua call *volume*, not
something specific to `mark_visited`'s script. (Claim-only's higher ceiling,
~18.2K vs. the combined path's ~13.4K, is consistent with `claim_next`'s
Lua being more expensive per call than `complete_claim`'s — it scans up to
`K` domains and touches more keys — so removing `complete_claim`'s calls
frees up more than half the total Lua budget, not exactly half.)

**B. `claim_visit`** — see §3.1; this is the production-shaped path and the
one the original baseline measures.

**C. `claim_delay`** — 10ms artificial `time.sleep()` between claim and
completion, workers=8, simulating a realistic (if still fast) HTTP
fetch/parse cost:

| Metric | Value |
|---|---|
| Throughput | 764/s (≈ 8 workers × 1/10ms, as expected) |
| Redis CPU | **5.9%** |

With any realistic per-URL processing time in the loop, Redis's own CPU
utilization collapses to essentially idle. This is the single most
important number for the recommendation in §6: **the frontier's ceiling is
irrelevant the moment real work exists between claim and completion.**

**D. Disabling result aggregation** — not run as a separate experiment.
Reading `distributed_benchmark.py:106-116`, each worker's `success_urls`
list is appended to in-memory (O(1) per URL) during the timed loop, and
only serialized to JSON *after* `frontier.close()`, outside the timed
`run_elapsed` window. There is no live aggregation inside the claim/complete
loop to disable — this candidate doesn't apply to this benchmark's design.

**E. Single vs. multi-worker** — covered by the full sweep in §3.1; scaling
is strong 1→4, saturates at 8, per Redis CPU.

### 3.4 Scale-invariance check

Re-ran `claim_visit`, workers=1, at the baseline's exact scale (200,000
URLs, 30s duration, 40 domains) instead of the smaller 150k/15s used for the
sweep:

| Run | claims/s | claim p50 (µs) | visit p50 (µs) | Redis CPU |
|---|--:|--:|--:|--:|
| 150k/15s sweep, w=1 | 4,346 | 155.6 | 59.5 | 24.5% |
| 200k/30s parity, w=1 | 4,488 | 156.3 | 60.1 | 24.0% |

Consistent to within noise. The sweep's smaller scale does not distort the
conclusion.

### 3.5 A confound checked and ruled out

Redis's default `save` policy (`3600 1 300 100 60 10000`) can trigger a
background `BGSAVE` fork mid-benchmark once enough writes accumulate. This
was observed (`rdb_bgsave_in_progress` sampled true) during the two
longer/lower-throughput single-worker runs (§3.1 w=1, §3.4), but not during
the shorter, higher-throughput w=2/4/8/16 runs (the save-trigger window
wasn't reached before the run ended). Irrelevant to the conclusion either
way: w=1's Redis CPU (24-24.5%) is far from saturated regardless.

---

## 4. Phase 5 — Optimization candidates, ranked

Per Amdahl's law: the frontier is not, and per §3.3(C) will not become, a
meaningful fraction of this crawler's end-to-end per-URL time once real
HTTP fetch/parse work is in the loop. Candidates are ranked accordingly.

```
HIGH IMPACT
  (none found)

MEDIUM IMPACT
  Shrink claim_next's Lua script: drop the trailing HGET for source_query
  (core/redis_frontier.py:249) and have callers fetch it lazily/separately
  instead of returning it inline from every claim.
    Measured share: one of ~9-10 Redis sub-operations inside claim_next's
    single round trip; a modest fraction of the ~19-27µs Lua execution time.
    Expected improvement: perhaps +10-15% on the Redis-CPU ceiling
    (~13.7K -> maybe ~15K/s) -- not transformative.
    Complexity: touches the Lua script's return contract and FrontierClaim
    plumbing in all 7 crawler backends.
    Risk: medium -- this is exactly the kind of "rewrite a working, tested
    Lua script" change this audit's brief says not to make without strong
    evidence of need.
    Worth doing now? No -- see §5. Evidence exists (§3.1/§3.3A), but the
    payoff doesn't clear the bar given §3.3(C).

LOW IMPACT
  Dedup the redundant domain-extraction work inside is_blacklisted()'s call
  graph (utils/url_utils.py). Measured via cProfile (20,000 calls): 76% of
  is_blacklisted()'s time is inside should_auto_blacklist(), which -- along
  with is_adult_content_url() and is_probable_ad_domain() -- each
  independently call _extract_registered_domain() on the same URL, so a
  single is_blacklisted() call does 3 redundant urlparse()+tldextract()+
  ipaddress.ip_address() passes over the same string.
    Measured cost: is_blacklisted() now costs ~64us/call post blacklist-fix
    (down from ~5.7ms pre-fix -- confirms the earlier fix worked; file I/O
    is no longer the story). Memoizing the extraction per-call would likely
    cut this to ~25-30us.
    Why it doesn't move this benchmark's ceiling: it's pure client-side
    Python CPU, and client CPU has slack up to and including 16 workers
    (Redis saturates first, per §3.1). Fixing it would not raise the
    measured throughput ceiling.
    Why it's still worth doing (separately): is_blacklisted() /
    get_link_priority() run in the real crawler's worker() loop once per
    claim and once per extracted link (frontier-optimization-audit.md
    §4.5) -- a page with 50 links pays this 50 extra times, and against the
    real datasets/domain_blacklist.txt (1,463 lines per the prior audit)
    the final match is a linear any(...endswith...) scan, not an O(1) set
    lookup, so the real-world cost is higher than this synthetic benchmark's
    38-domain blacklist shows.
    Complexity: low -- pure in-function memoization, no behavior change.
    Risk: low.
    Worth doing now? As its own item, yes -- but it is a real-crawler CPU
    efficiency fix, not a frontier-throughput fix. Not executed as part of
    this audit (out of scope for "raise the ceiling"); flagged for a future,
    separate pass.

NOT WORTH OPTIMIZING
  - Lua script changes beyond the one HGET (§ above already covers the only
    real lever; nothing else in claim_next/complete_claim showed up as a
    disproportionate cost).
  - redis-py client tuning / connection pooling -- one connection per
    process is already sufficient at this concurrency; no evidence of
    client-side connection contention.
  - Multiprocessing/IPC / result aggregation -- happens after the timed
    window (§3.3D); nothing to optimize.
  - SQLite / url_database -- not wired into this benchmark at all
    (tests/benchmarks/common.py's build_frontier() never passes one).
  - Imports, cosmetic Python, dict lookups, string formatting -- no
    profiling evidence any of these are relevant; not touched, per the
    audit's own instructions.
```

---

## 5. Recommendation

```
~13.4-13.8K URLs/s is good enough. Do not spend another optimization cycle
on the frontier's throughput ceiling. Proceed with the roadmap.
```

The ceiling is real, reproducible (§3.1, §3.4), and has a specific,
measured cause: Redis's own single-threaded command-execution loop
saturating on Lua call volume at ≥8 concurrent workers (§3.1, §3.3A). The
one lever that would move it — shrinking `claim_next`'s Lua script — carries
real correctness risk (touches a working, tested, atomic script across 7
crawler backends) for a number that the `claim_delay` experiment (§3.3C)
proves nothing downstream will ever pressure: real HTTP fetch/parse work
(even a lightweight 10ms) drops Redis CPU from saturation to 5.9%. Per this
audit's own instructions ("the objective is NOT to maximize an artificial
benchmark at the expense of crawler correctness," "don't rewrite working
Lua scripts without evidence"), that risk is not justified by that payoff.

No Phase 6 experiment (make one optimization, re-benchmark, keep if
meaningful) was run, because none of the identified candidates passed their
own justification test:
- The one candidate that *would* move this specific ceiling (the Lua
  script) isn't worth the correctness risk given §3.3(C).
- The one candidate that's otherwise worth doing (the blacklist dedup)
  wouldn't move this ceiling at all, since client CPU isn't the binding
  resource (§3.1).

## 6. Roadmap decision

```
Proceed with the roadmap. Skip another optimization cycle.
```

```
Fix Redis outage semantics      <-- NEXT
    v
Failure visibility
    v
Domain starvation test
    v
SQLite batching/duplicate writes
    v
Final crawler benchmark
    v
CRAWLER COMPLETE
    v
FINGERPRINTER
```

`frontier-optimization-audit.md` §4.1-§4.3 already identified this as a real
P0/P1 correctness gap (`has_pending()`/`get_status_counts()` treat any Redis
error as "nothing pending," which can trigger a premature full-crawler
shutdown during a transient outage; `add_url`/`mark_*` silently swallow
failures with no caller-visible signal). It is unrelated to throughput and
more urgent than a further ~10-15% squeeze on a ceiling nothing in this
crawler will ever hit.

**Optional, non-blocking, separate follow-up:** the `is_blacklisted()`
redundant-extraction dedup (§4, LOW IMPACT) is a real, cheap, low-risk win
for the *real crawler's* per-claim and per-link CPU cost — worth doing
whenever convenient, but it does not gate or relate to the roadmap above.

---

## 7. Things confirmed NOT to touch

Consistent with `frontier-optimization-audit.md` §13, this audit adds no
new items to that list and removes none — everything found here is either
"not worth doing" or "worth doing elsewhere, not here":

- The Redis keyspace, Lua script design, and claim/lease/token model —
  still correct, still atomic, still not the source of any correctness
  problem. The one *measured*, real lever inside it (the trailing `HGET` in
  `claim_next`) is a throughput micro-optimization with no current
  justification (§5), not a correctness concern.
- `domain_scan_limit` / the `K`-bounded scan — untouched and out of scope
  for this audit (already covered by the prior audit's §4.6/§8.3).
- The benchmark harness's design (one Lua call per operation, synthetic
  workload, process-per-worker model) — confirmed to already isolate what
  it needs to; the gaps this audit filled (per-operation latency, proper
  Redis CPU deltas) were addressed with an external, temporary probe rather
  than modifying the committed suite.
