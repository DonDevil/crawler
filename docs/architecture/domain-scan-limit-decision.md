# `domain_scan_limit` Production Decision (Step 8B)

Status: **decision implemented.** Continues from
`docs/architecture/domain-starvation-audit.md` (Step 8) and
`docs/architecture/domain-scan-window-design.md` (Step 8A). This document
records the production configuration decision those investigations led to,
what was actually changed, and the concrete conditions under which the
deferred eligible-domain-index redesign should be revisited.

---

## Current decision

```
domain_scan_limit = 250   (was 50)
```

Configurable, explicitly overridable, no other scheduling behavior changed:
the K-bounded scan algorithm itself, priority semantics, rate limiting, and
retry/recovery are all byte-for-byte unchanged. This is a configuration
change, not an architecture change.

### Why

- The production seed file alone already spans ~50 distinct domains
  (`seeds/piracy_sites.txt`, measured in Step 8) — already at the *old*
  default before any link-discovery expansion.
- Production's intended architecture is multiple independent crawler
  systems, each potentially running multiple workers, all coordinating
  through one shared Redis frontier — the K-window's visibility limit is a
  property of the shared Redis keyspace, so it applies to the fleet's
  combined active-domain population, not any single worker's view of it.
- The old K=50 boundary was uncomfortably close to the *already-known*
  minimum workload (50 seed domains), leaving effectively zero headroom
  before link discovery could push past it.
- Step 8A measured 250 to be cheap: worst-case `claim_next` latency (every
  one of the K candidates simultaneously present and rate-gated — the true
  worst case, not the common case) is **0.67ms mean / 0.93ms max** at
  K=250, re-confirmed directly in this task (§ Benchmarks below) — small
  relative to real per-request costs (HTTP fetch time, already established
  in `throughput-ceiling-audit.md` §3.3C to dwarf this by orders of
  magnitude) and to Redis's own single-threaded budget.
- 250 avoids jumping straight to 500/1000, whose worst-case cost (1.3ms /
  2.9ms respectively, all measured) is unjustified without evidence that
  250 is insufficient — matching the "do not simply keep increasing K
  indefinitely" principle below.
- The eligible-index redesign (Step 8A) remains the architecturally correct
  long-term fix, but building it now would be solving a problem not yet
  confirmed to occur at this crawler's actual scale — exactly the
  over-engineering Step 8A's own brief warned against.

---

## Configuration mechanism

Single, existing mechanism — confirmed by tracing every reference to
`domain_scan_limit` in the codebase before changing anything (no duplicate
configuration source was created):

```
config.yaml (crawler.frontier.domain_scan_limit)
    ↓ loaded by
core/config.py (FrontierConfig.domain_scan_limit, pydantic default)
    ↓ read by
core/crawler_manager.py (passes frontier_config.domain_scan_limit through)
    ↓ constructs
core/redis_frontier.py (RedisURLFrontier.__init__'s domain_scan_limit param)
```

`config.yaml`'s value is authoritative for a normal run; `FrontierConfig`'s
pydantic default is what applies if `config.yaml` is absent or omits the
key; `RedisURLFrontier`'s own constructor default is a third, independent
fallback for code that instantiates it directly (tests, benchmarks,
`tests/benchmarks/common.py`'s own CLI tooling) without going through
`CrawlerManager`/`config.yaml` at all. All three were already required to
stay in sync by existing convention (every other Redis-frontier tuning
knob — `rate_limit`, `max_retries`, `lease_ttl`, `reclaim_batch_size` — is
mirrored the same way between `FrontierConfig` and the constructor
signature); this change follows that same convention rather than
introducing a new one. `tests/benchmarks/common.py`'s own `--domain-scan-limit`-style
CLI default (used only by ad hoc benchmark/investigation tooling, never by
production) was deliberately left at 50, since it exists specifically to
let an operator run K comparisons like Step 8A's — changing its default
would work against that purpose.

---

## Future trigger

`domain_scan_limit` is currently an intentional bounded-work
safety/performance parameter, not a permanent guarantee that all active
domains are globally visible. **The current interim policy is K=250.** Do
not simply keep increasing K indefinitely — every increase has a measured,
linear worst-case Lua cost (§ Benchmarks), and each one only moves the
boundary rather than removing the underlying limitation (Step 8A §4,
Approach A).

**Revisit the eligible-domain-index architecture (Step 8A §4 Approach C+D)
if any of the following is observed from real crawler telemetry, not
projected from this investigation's synthetic benchmarks:**

- Active domains (per the new `get_domain_scan_telemetry()` sample, below)
  regularly approach or frequently exceed 250 during real runs.
- Redis CPU becomes a sustained bottleneck attributable to domain scanning
  specifically (distinguishable from the already-documented, already-
  accepted Lua-call-*volume* ceiling in `throughput-ceiling-audit.md` — that
  ceiling is unrelated to K and is not a trigger by itself).
- Claim latency degrades materially in a way traceable to `claim_next`'s
  worst-case branch (many top-ranked domains simultaneously gated), not to
  network/fetch-side costs.
- Multiple concurrent crawler systems sharing one Redis frontier create
  contention that makes the current K-window's cost (paid by every worker,
  every call, fleet-wide) expensive in aggregate even where a single
  worker's view of it looks fine in isolation.

None of these is currently observed — this section documents the
escalation rule, not a present finding.

---

## Read-only production telemetry

**Added** (`core/redis_frontier.py`, `RedisURLFrontier.get_domain_scan_telemetry()`):
a single pipelined `SCARD domains:active` (1 round trip, O(1), never a
`SCAN`), returning:

```python
{
    "active_domains": int,
    "queued_domains": int,        # == active_domains, see limitation below
    "rate_gated_domains": None,   # not cheaply available, see limitation below
    "domain_scan_limit": int,
    "exceeds_domain_scan_limit": bool,
}
```

**Wired into `CrawlerManager._recovery_loop`** (`core/crawler_manager.py`) —
deliberately piggybacked on the *existing* periodic recovery sweep
(`recovery_interval`, default 30s) rather than a new loop, a per-claim hook,
or any additional Redis polling. This satisfies "cheap enough not to
materially affect performance" by construction: it is bounded to the same
low frequency the recovery sweep already runs at, adds exactly one O(1)
Redis command to that existing tick, and is a complete no-op for the local
frontier (`AsyncFrontier.get_domain_scan_telemetry()` returns `None` for any
backend that doesn't implement it, mirroring the existing
`reclaim_and_promote` optional-method pattern).

Behavior:
- Logs a `WARNING` when `active_domains > domain_scan_limit` (the moment
  it's actually relevant), `DEBUG` otherwise (no log noise in the normal
  case).
- Tracks `self._peak_active_domains` across the run's lifetime; logged once
  at shutdown (`Peak active domains observed: N (domain_scan_limit=K)`) —
  gives an operator a single number to check after any run without needing
  to grep for warnings.

**Documented limitation, not implemented**: `rate_gated_domains` is always
`None`. The current Redis keyspace has no aggregate index of which domains
are currently rate-gated — only a per-domain `domain:{d}:next_time` STRING
key each. Counting them cheaply would require either a `SCAN` over
`domain:*:next_time` (an operation this codebase deliberately avoids on
principle everywhere else, and would not be "cheap") or the eligible-index
redesign's `gated` ZSET (Step 8A §4/§5) — which is exactly the deferred
architecture change, not something to build piecemeal for telemetry alone.
This is stated explicitly rather than worked around with an expensive
mechanism, per this task's own instruction.

`queued_domains` is reported identically to `active_domains`: in the
current keyspace, `domains:active` already means "has at least one queued
URL" — there is no separate, cheaper, or differently-scoped "queued"
concept to report beyond that.

---

## Benchmarks (focused, not a repeat of the 200K campaign)

All measurements below are new to this task (re-confirming, not assuming,
Step 8A's numbers still hold at the actual chosen value) against the real,
unmodified `RedisURLFrontier`, isolated benchmark namespaces, cleared after
use.

### Worst-case `claim_next` latency (all K candidates present and rate-gated)

| K | Mean | Max |
|---:|---:|---:|
| 50 | 0.173 ms | 0.275 ms |
| **250 (new default)** | **0.673 ms** | **0.931 ms** |
| 500 | 1.323 ms | 1.411 ms |

Matches Step 8A's independently-measured numbers closely (0.166/0.370ms at
K=50, 1.449/1.649ms at K=500) — same linear scaling, no regression, no
surprise.

### Scan-window visibility boundary, reproduced at the new K=250

- **Continuous replenishment, 15 domains (well under K=250)**: victim still
  starved (0 claims) — confirms this is ordinary, intentional strict-priority
  behavior (Step 8's Mechanism J), **unrelated to and unaffected by** the K
  change, exactly as expected.
- **Finite backlog, 260 domains (just over K=250), no replenishment**:
  victim claimed correctly, last, at claim #261 — confirms the K-window
  itself does not block a finite/exhaustible backlog once it drains below
  K, the same self-resolving property Step 8A measured at K=10.
- **Continuous replenishment, 260 domains (just over K=250)**: victim
  starved (0/600 claims) — confirms the K-window failure mode Step 8A
  demonstrated **still exists, exactly as documented, just at the new,
  higher, now-measured threshold** (260 simultaneously-replenished
  better-ranked domains, not 15). This is the expected, accepted outcome of
  "raise K," not a regression — the interim policy explicitly does not
  claim to remove this mechanism, only to move it further from this
  crawler's currently-known workload.

---

## Tests

- `tests/domain_scan_limit_config_test.py` (new): 5 focused tests —
  `FrontierConfig` default is 250; `RedisURLFrontier`'s constructor default
  mirrors it (250) without opening a Redis connection; `config.yaml` loads
  to 250; `CrawlerManager` propagates an explicit override (77 in the test)
  through to the live `RedisURLFrontier` instance; `CrawlerManager`
  propagates the unset default (250) through the same path. The last two
  are the ones that actually prove wiring, not just the constant.
- Full existing suite re-run for regression: `tests/frontier_test.py`,
  `tests/redis_frontier_test.py` (priority ordering, rate-limit skip,
  concurrent-claim safety, crash/reclaim, retry/backoff — all untouched
  behaviorally by this change), `tests/frontier_redis_failure_semantics_test.py`,
  `tests/crawler_manager_recovery_test.py` (exercises the exact
  `_recovery_loop` this task added telemetry sampling to, against a real
  Redis frontier, not a mock), `tests/manager_test.py`.
- Full repo suite (`pytest tests/`) also re-run: 156 passed, 2 skipped
  (Redis-dependent tests skip cleanly when Redis is unavailable, as
  designed), 1 failed —
  `tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`.
  Confirmed **pre-existing and unrelated**: this file was not touched in
  this task or Step 8/8A (last modified 2026-08-09, before this work
  began), the test itself concerns single-domain concurrent-claim
  thread-pool bounding (irrelevant to `domain_scan_limit`, which only
  matters when multiple *distinct* domains compete for the top-K window),
  and re-running it in isolation reproduces the same intermittent
  pass/fail (failed, failed, passed across three consecutive runs) —
  matching the "previously identified unrelated flaky test" already noted
  as deselected in this project's Step 7 status.

---

## Files changed

- `config.yaml` — `domain_scan_limit: 50` → `250`, with an inline comment
  pointing at this decision doc.
- `core/config.py` — `FrontierConfig.domain_scan_limit` default `50` → `250`,
  with an explanatory comment (the same principle stated in the "future
  trigger" section above, kept next to the value it governs).
- `core/redis_frontier.py` —
  - Constructor default `domain_scan_limit: int = 50` → `250` (mirrors the
    `FrontierConfig` default per existing convention, as `rate_limit`/
    `max_retries`/etc. already do).
  - `get_next_url`'s docstring `(default 50)` → `(default 250)`, plus a
    pointer to the design doc.
  - New method: `get_domain_scan_telemetry()`.
- `core/frontier_executor.py` — new `AsyncFrontier.get_domain_scan_telemetry()`
  passthrough (optional-method pattern, mirrors `reclaim_and_promote`);
  added `Optional` to the existing `typing` import.
- `core/crawler_manager.py` — new `_peak_active_domains` instance attribute;
  new `_sample_domain_scan_telemetry()` method; one call to it added inside
  the existing `_recovery_loop` (after each `reclaim_and_promote` sweep,
  same cadence); one summary log line added to `run()`'s shutdown path.
- `tests/domain_scan_limit_config_test.py` — new (5 tests, see above).
- This document.

**Not changed** (per explicit scope): any Lua script, any Redis key
structure, priority semantics, rate-limit semantics, retry/recovery
semantics, the K-bounded scan algorithm itself, adaptive-K logic, or the
eligible-domain-index design — all remain exactly as Step 8/8A left them.

---

## Architectural principle for future performance work

Recorded per this task's instruction, to apply from here forward:

**Evaluate at two levels:**

- **Level 1 — current benchmark.** What is the measured bottleneck on the
  current development machine, under realistic load shapes?
- **Level 2 — production distributed architecture.** What happens under
  `multiple machines × multiple workers × shared Redis × long-running
  crawling`? A cost that's negligible per-operation can still matter if
  that operation happens once per URL, once per claim, or once per worker,
  multiplied across an entire fleet over a multi-day run.

**But**: do not use hypothetical future scale as justification for
speculative rewrites on its own. Every change should be grounded in

```
measured current behavior + plausible production scaling impact + proportional engineering effort
```

together — not any one of the three alone. This is exactly the reasoning
this task applied to `domain_scan_limit` itself: Level 1 (Step 8A's
single-host measurements) showed 250 is cheap; Level 2 (production's
multi-system, shared-Redis architecture) is why the fix couldn't stop at
"well, HTTP is slower anyway" and needed real headroom against a fleet-wide
shared resource; proportional effort is why the answer was "raise a config
value and add cheap telemetry," not "rewrite the Lua scheduler," absent
evidence the latter is actually needed yet.
