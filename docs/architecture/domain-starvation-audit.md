# Domain Starvation Audit (Step 8)

Status: **audit only — no production code changed.** This document formalizes
the current Redis/local frontier scheduling model, then reproduces or
disproves each starvation mechanism in
`docs/architecture/frontier-optimization-audit.md` §4.6/§8.3 with a
deterministic tool (`tests/benchmarks/domain_starvation.py`), against both
frontier backends. All measurements below were run against this repo's real
`RedisURLFrontier`/`URLFrontier` implementations through their public API —
no internals were mocked or monkeypatched.

---

## 1. Current scheduling model

### 1.1 Priority

- **Direction, verified by code and by measurement (`finite` scenario,
  §9.1): lower numeric priority is claimed first.** Score formula
  (`core/redis_frontier.py:170`, mirrored by the local frontier's tuple
  ordering, `core/url_frontier.py:84,101`):
  `score = priority * 1_000_000 + seq`. Both frontiers claim in ascending
  score order (`ZRANGE ... 0 0` in Lua; `heapq` min-pop locally), so
  priority 1 is claimed before priority 10. Confirmed against real config
  values too: seed URLs get priority 12 (8 for onion), unfinished-pending
  URLs get priority 3, and search-engine discovery assigns `torch=0`
  (best) through `yandex=7` (worst) — `core/crawler_manager.py:213-219`,
  `core/config.py:91-98` — all consistent with "lower number = more
  urgent."
- **Priority is global**, not per-domain: it orders every domain's queue
  head against every other domain's queue head through one shared score
  space (`domain_heads` / the local `priority_queue` heap).
- **Stored per-URL** at insertion time (`meta:{url}` hash field `priority`,
  Redis; the third tuple element in `domain_queues`, local) —
  `core/redis_frontier.py:181-183`, `core/url_frontier.py:84`.
- **Evaluated at both insertion and claim time**: the score is computed
  once at `add_url` and is immutable afterward for that specific queued
  entry — there is no "re-prioritize while queued" operation in either
  backend.
- **After retry**, the same `priority` value is reused (read back from
  `meta:{url}` in Redis, carried on the `FrontierClaim` locally) —
  `core/redis_frontier.py:399`, `core/url_frontier.py:231`. A retried URL
  gets a **new**, larger `seq` (it's a fresh `ZADD`/heap-push), so among
  same-priority URLs it goes to the back of that priority tier, not the
  front.
- **After recovery/reclaim**, same as retry — `reclaim_and_promote`'s phase
  (b) reads `priority` from `meta:{url}` and reuses it unchanged
  (`core/redis_frontier.py:399-410`).

### 1.2 Domain scheduling

- Domain = URL's `netloc` (`urlparse(cleaned).netloc`).
- Each domain has one ready queue (`domain:{d}:queue` ZSET in Redis;
  `domain_queues[domain]` deque locally), ordered by `(priority, seq)`.
- **Cross-domain selection is global, not per-worker** — every claimer
  (thread/process in Redis; the single per-process scheduler loop locally)
  competes for the same `domain_heads` index / `priority_queue` heap.
- **A domain cannot "occupy the scheduler repeatedly" beyond what its own
  priority earns it** — every single claim re-evaluates the full candidate
  set from scratch; there is no session affinity or "stick with the last
  domain" behavior in either backend.
- **An empty domain is actively removed**, not left to block scanning:
  Redis self-heals a stale `domain_heads` entry inline
  (`core/redis_frontier.py:211-216`) and removes it as soon as a claim
  drains the queue to empty (`:230-233`); the local frontier discards an
  empty `domain_queues` entry (`_requeue_or_drop_domain`,
  `core/url_frontier.py:131-135`) and never re-pushes it to the heap.
- **The one structural difference between backends**: Redis's `claim_next`
  only examines the top `K` (`domain_scan_limit`, default 50) entries of
  `domain_heads` per call — a hard, bounded-worst-case-Lua-runtime
  visibility window (`core/redis_frontier.py:204`). The local frontier's
  `get_next_url` has **no such bound**: it pops the *entire* heap if
  necessary, setting aside every rate-gated domain in `blocked_domains` and
  re-pushing them all at the end (`core/url_frontier.py:151-182`). This is
  documented as an intentional, deliberate tradeoff in
  `docs/architecture/frontier-adr.md` §6 (a Lua script needs a bounded
  worst case; an in-process Python loop does not) — see §5 below for what
  this actually costs in practice.

### 1.3 Rate limiting

- `rate_limit` is a **single global float**, applied identically to every
  domain (`core/redis_frontier.py:111`, `core/url_frontier.py:46`,
  `core/config.py:108`'s `CrawlerConfig.rate_limit` default `1.0`). There is
  **no per-domain rate-limit configuration** anywhere in this codebase —
  the audit brief's Case C ("domain A rate limit = 1s, domain B rate limit
  = 0") is not currently expressible; every domain shares one crawler-wide
  value.
- On claim, `next_allowed_time = now + rate_limit` is written
  (`domain:{d}:next_time` in Redis, `domain_next_time[domain]` locally) —
  `core/redis_frontier.py:238`, `core/url_frontier.py:171`.
- **A rate-gated domain is skipped, not blocked**, in both backends: Redis's
  Lua loop `continue`s to the next `domain_heads` candidate without
  removing the gated domain (`:218-223`); the local frontier pops it off
  the heap, appends it to `blocked_domains`, and keeps popping
  (`core/url_frontier.py:161-163`), re-pushing every blocked domain at the
  very end of the call regardless of which branch returned. **Measured**:
  §9.2 below.
- Rate-limit state lives in the frontier backend itself (Redis key /
  in-process dict), so it **is** global across workers for the Redis
  backend (shared Redis state) and trivially consistent for the local
  backend (single process, single dict).
- **Workers cannot race on the same domain's rate gate**: the entire
  pick-domain → check-gate → pop-URL → set-`next_time` → issue-claim
  sequence is one atomic Lua script server-side (Redis) or runs on a single
  thread with no `await` in the middle (local) — confirmed by the existing
  `test_get_next_url_no_duplicates` / `test_concurrent_claims_same_domain_never_duplicate`
  tests and re-confirmed by this audit's own `multi-worker` scenario (§9.6).
- **`rate_limit = 0` is not special-cased anywhere** — checked directly:
  Redis's gate is `next_time > now` (strict), so `next_time = now + 0`
  fails that check on the very next call (time has already advanced by a
  few microseconds); the local frontier's gate is `now < domain_next_time`
  with the identical property. §9.7 confirms no anomalous behavior at
  `rate_limit=0` beyond it being, correctly, "no rate gate at all."
- **A rate-gated top-priority domain within the visible candidate set never
  blocks a lower-priority eligible domain from being claimed** — measured
  directly (§9.2), and this is the *only* fairness mechanism this scheduler
  has (§1.4).

### 1.4 Fairness

**There is no fairness mechanism beyond rate-limit-triggered skipping.**
No round-robin, no weighted round-robin, no aging, no priority decay, no
time-based promotion, no explicit domain rotation, no max-consecutive-claims
cap. This is not an oversight this audit is flagging as a gap — it is the
literal, documented design in `docs/architecture/frontier-adr.md` §6
("`domain_heads` ... picks the globally-best eligible domain") and §1.1's
reading of `core/redis_frontier.py`/`core/url_frontier.py` confirms the ADR
is faithfully implemented. **The scheduler's only means of ever giving a
lower-priority domain a turn is the side effect of the higher-priority
domain being temporarily rate-gated or fully drained.** Section 4 formalizes
exactly when that side effect is and isn't sufficient.

---

## 2. Starvation, precisely defined

Per the audit brief: ordinary priority delay is not starvation. This audit
treats a result as **starvation** only when a domain with valid, currently
non-rate-gated queued work receives **zero claims across an entire
deterministic run**, or when its wait is unbounded as a function of a
variable the operator does not control (e.g., another domain's link
discovery rate) rather than bounded by its own priority tier draining.

---

## 3. Test tool

`tests/benchmarks/domain_starvation.py` — a manual CLI script, not
pytest-collected (matches `priority_ratelimit.py`'s convention: bootstraps
`sys.path`, reuses `tests/benchmarks/common.py` for frontier construction,
blacklist isolation, and synthetic-URL helpers). Seven scenarios
(`finite`, `rate-limit-skip`, `replenish`, `scan-limit-window`, `retries`,
`multi-worker`, `recovery`), each deterministic (claims to exhaustion or a
fixed claim count/poll budget, never "run for N seconds and hope"). A shared
`compute_fairness()` derives, per domain: claim count, first/last claim
offset, max wait between its own claims, % of total claims, and the longest
run of consecutive claims made to a single competing domain.

All runs below used an isolated, empty temporary blacklist file
(`common.isolate_blacklist()`) and a dedicated Redis namespace/db
(`bench_starvation`, `redis_db=2`) — never the production keyspace.

---

## 4. Measured results

### 4.1 Scenario 1 — finite priority ordering

`finite --high-count 5 --low-count 3`, local frontier. Both domains fully
drained (8/8 claims); domain "high" (priority 1) claimed exhaustively before
domain "low" (priority 10) began:

| Domain | Priority | Seeded | Claims | First claim | Last claim |
|---|---:|---:|---:|---:|---:|
| high | 1 | 5 | 5 | step 1 | step 5 |
| low | 10 | 3 | 3 | step 6 | step 8 |

Result: **not starvation** — this is exactly the intended strict-priority
ordering, and every URL was eventually claimed (`all_urls_eventually_claimed: true`).

### 4.2 Scenario 2 — rate-limit skip behavior

`rate-limit-skip`, local frontier, `rate_limit=60`: domain `hot` (priority
1) claimed once, becomes rate-gated for 60s; a second `hot` URL plus a
`cold` URL (priority 50) are then added. Next claim: **`cold`**, not
blocked behind the 60s-gated `hot`. `skipped_not_blocked: true`. Identical
result against Redis (`test_rate_limited_domain_does_not_block_lower_priority_eligible_domain`
in `tests/redis_frontier_test.py:244` already covers this; this audit's run
reproduces it with the standalone tool too).

### 4.3 Scenario 3 — continuous high-priority replenishment (the critical case)

`replenish`, domain A (priority 1) continuously topped up by 5 URLs every
time it's claimed; domain B (priority 10) seeded with 10 URLs once.

| `rate_limit` | Frontier | Claims run | A claims | B claims | B starved? |
|---:|---|---:|---:|---:|---|
| 0.0 | local | 300 | 300 | 0 | **Yes** |
| 0.0 | redis | 300 | 300 | 0 | **Yes** |
| 0.05 | local | 60 | 50 | 10 (all) | No — B's first claim at offset 0.0008s |

At `rate_limit=0`, B received **zero** claims out of 300 in both backends —
identical result, confirming this is not a backend-specific bug but a
direct, symmetric consequence of strict global priority with an
unboundedly-replenished top domain and no rate gate ever engaging. At
`rate_limit=0.05`, B was fully drained (10/10) with sub-millisecond first-claim
latency, because A's own rate gate opened a window every ~52ms during which
B was the best *eligible* candidate. **This is the entire fairness story of
this scheduler**: rate limiting is not a politeness feature that happens to
also produce fairness — at `rate_limit>0` it *is* the only fairness
mechanism, and at `rate_limit=0` there is none.

### 4.4 `domain_scan_limit` (K) visibility window — Redis only

Reproduces `frontier-optimization-audit.md` §4.6/§8.3 directly.
`scan-limit-window --domain-count 15 --domain-scan-limit 10`: 15 filler
domains (priority 1, continuously replenished after every non-victim claim)
vs. one `victim` domain (priority 100, 5 URLs, seeded once).

- Continuous replenishment (`--num-claims 400`): **victim received 0/400
  claims.** All 15 fillers were claimed roughly evenly (24-27 claims each)
  — confirmed the `K`-window itself rotates fairly *among* the domains
  inside it, but a domain whose score never ranks in the top `K` is
  invisible to `claim_next` regardless of how idle it's been.
- **Finite fillers** (separate ad hoc run: 15 domains × 20 URLs each, no
  replenishment, same victim): victim's first claim landed at **claim
  #301**, immediately after the 300th (last) filler URL drained — i.e. the
  instant fewer than `K=10` filler domains remained non-empty, the victim
  entered the visible window and was claimed normally. All 5 victim URLs
  were eventually claimed.
- **Local-frontier control** (ad hoc, no `domain_scan_limit` concept, same
  15-filler continuous-replenishment setup): victim also received **0/400**
  claims. This isolates the mechanism precisely: the local frontier has no
  `K` bound and scans its *entire* heap every call, yet the victim still
  starved — because with 15 domains permanently ranked better and
  permanently non-empty, ordinary strict priority alone (§4.3's mechanism)
  is already sufficient to starve it. The `K`-window does not add a new
  failure mode on top of strict-priority starvation; **it only changes how
  few permanently-replenished better-ranked domains are needed to trigger
  the same starvation, and it adds a second, independent trigger**: even
  with *finite* higher-priority backlogs, a domain ranked outside the top
  `K` is invisible until enough of those `K` domains fully drain, whereas
  the local frontier would make it visible (though still not claimable
  ahead of better-priority work) immediately.

**Production relevance, measured, not assumed**: this crawler's active seed
file (`seeds/piracy_sites.txt`) alone contains 51 URLs across **50 distinct
domains** — already at the default `domain_scan_limit` of 50 before any
link-discovery expansion runs. This confirms the prior audit's "plausible"
framing (§4.6: "likely well over 50 distinct domains once link discovery is
running") with a concrete number from this repo's actual seed data.

### 4.5 Retries

`retries`, both backends, `max_retries=5`, small backoff (`0.05-0.2s`): a
3-URL domain A (priority 1) fails every attempt and exhausts retries; a
10-URL domain B (priority 10) is seeded once and always succeeds.

| Frontier | Total claims | A claims | B claims | B starved? |
|---|---:|---:|---:|---|
| local | 25 | 15 (3 URLs × 5 attempts) | 10 (all) | No |
| redis | 13 | 3 (hit backoff before max_retries could all fire in-window) | 10 (all) | No |

B was fully drained in both runs. This matches the code read directly
(§1.1): a failed claim goes to `retry_scheduled` (Redis) / the local
`_retry_heap`, which **removes the URL from `domain_heads`/`priority_queue`
entirely** until its backoff expires — a backed-off domain is exactly as
invisible to scheduling as an empty one, so it cannot compete with (let
alone block) an eligible domain during its backoff window. Repeated retries
cannot starve another domain; they can only ever compete once actually
re-promoted, at which point they're subject to the same strict-priority
rules as any other queued work.

### 4.6 Multi-worker (1/2/4/8 concurrent claimers, Redis)

`multi-worker --high-count 200 --low-count 20 --worker-counts 1,2,4,8`:

| Workers | Total claims | Duplicate claims | Both domains fully drained |
|---:|---:|---:|---|
| 1 | 220 | 0 | Yes |
| 2 | 220 | 0 | Yes |
| 4 | 220 | 0 | Yes |
| 8 | 220 | 0 | Yes |

Zero duplicate claims at every concurrency level (consistent with the
existing claim-safety test suite), and priority/fairness semantics were
unaffected by concurrency — every worker draws from the same atomic
`domain_heads` index, so N concurrent claimers behave like one claimer
running N times faster, not like N independent schedulers with divergent
views. **Concurrency does not change starvation risk in either direction**
for a finite workload; it does not need to be considered separately from
the single-worker scenarios above for fairness purposes (duplicate-claim
safety, which *is* a distinct concurrency concern, was already validated
pre-Step 8 and is reconfirmed here as a side effect).

### 4.7 Recovery / reclaim (Redis only)

`recovery --lease-ttl 1.0 --recovery-cycles 4`: domain A (priority 1, 1 URL)
is claimed and then abandoned (simulating a crashed worker) every cycle;
`reclaim_and_promote` is called after each lease expiry. Domain B (priority
10, 5 URLs) is claimed opportunistically between cycles.

| Domain | Claims | Starved? |
|---|---:|---|
| A (repeatedly reclaimed) | 3 | — |
| B (fixed, low priority) | 5 / 5 | **No** |

B was fully drained. This matches the code read (§1.1/§1.2): a claimed URL
is removed from `domain_heads` the instant it's claimed and stays removed
for the entire time it's inflight (including while its lease is silently
expiring) — an abandoned claim makes that domain's queue *more* absent from
scheduling, not less, until reclaim explicitly requeues it. Repeated
reclaim/recovery of one domain cannot starve another.

---

## 5. Mechanism-by-mechanism classification

| # | Mechanism | Status | Evidence |
|---|---|---|---|
| A | Global priority queue permanently prefers a continuously replenished domain | **PRESENT** | §4.3 — this is the core, intended strict-priority behavior; see §6 for whether it's a bug. |
| B | Rate-limit scheduling causes repeated reconsideration of the same domain | **NOT PRESENT** as a starvation cause | §4.2, §4.3 (rate_limit>0 row) — reconsideration is exactly what *prevents* starvation here, working as designed. |
| C | A temporarily-ineligible high-priority domain blocks lower-priority domains | **NOT PRESENT** | §4.2 — explicitly measured skip-not-block behavior, both backends. |
| D | Retry/requeue pushes the same domain back ahead indefinitely | **NOT PRESENT** | §4.5 — backoff removes a domain from scheduling entirely until due; retried work re-enters at the back of its priority tier (new `seq`), never "ahead." |
| E | Multiple workers race and repeatedly consume one domain | **NOT PRESENT** | §4.6 — atomic Lua claim serializes all cross-worker scheduling decisions; no duplicate claims, no skewed domain distribution observed at 1/2/4/8 workers. |
| F | A domain's `next_allowed` timestamp is updated incorrectly | **NOT PRESENT** | §1.3, `core/redis_frontier.py:238`/`core/url_frontier.py:171` — read directly; `now + rate_limit`, unconditional, no double-application or missed-reset found. `rate_limit=0` produces no special-case bug (§4.3 replenish@0.0 row ran cleanly with no anomalies beyond the expected total-dominance result). |
| G | Priority ordering reversed/inconsistent between insertion and claim | **NOT PRESENT** | §4.1, `test_priority_ordering_across_domains_via_domain_heads` — insertion score and claim order verified identical direction in both backends. |
| H | Recovery/reclaim reintroduces URLs with priority that dominates forever | **NOT PRESENT** | §4.7 — reclaimed/requeued URLs reuse their original priority (§1.1) and compete under ordinary strict-priority rules once requeued; repeated reclaim of A never blocked B. |
| I | An empty/ineligible domain queue not removed or skipped correctly | **PRESENT, but only as the documented `K`-bounded visibility window (Redis only)** | §4.4, `core/redis_frontier.py:204`. Not a "queue not removed" bug (empty queues *are* removed correctly — §1.2) — it's a fixed-size candidate window that never even looks at domains ranked outside it. Confirmed transient (resolves once enough better-ranked domains drain, §4.4's finite-filler run) but permanent under continuous replenishment of ≥K better-ranked domains (§4.4's continuous run). Local frontier is immune to this specific mechanism (unbounded scan, §4.4's local-control run) but suffers the *same end state* via mechanism A once enough domains are permanently non-empty. |
| J | Continuously growing high-priority workload makes low-priority work mathematically unreachable | **PRESENT — by design, not a bug** | §4.3. This is the direct, intended consequence of strict global priority with zero fairness mechanism (§1.4) and is explicitly the ADR's documented design (`frontier-adr.md` §6: "picks the globally-best eligible domain"), not an accident. |

---

## 6. Priority policy vs. starvation — which policy does this system implement?

**Strict priority**, unambiguously. `docs/architecture/frontier-adr.md` §6
describes the scheduler as picking "the globally-best eligible domain" with
no aging, decay, or fairness term anywhere in the design or the
implementation (§1.4). There is no ambiguity to flag under §14 of the audit
brief — the ADR does specify a policy, and the implementation faithfully
matches it. Given that, mechanism J (§5) is a **mathematical consequence of
an intentional design choice**, not a defect: "infinite high-priority
workload + finite lower-priority workload + strict priority ⇒ lower
priority may never execute" is exactly what strict priority means, and this
codebase chose strict priority deliberately.

**What *is* worth a real decision** (separate from "is strict priority
correct") is mechanism I (§4.4, §5): the `domain_scan_limit` window is not
a fairness mechanism and was never intended as one (`frontier-adr.md` §6
frames `K` purely as a Lua-runtime bound) — but its *side effect* is that a
domain can be invisible to scheduling even during periods when it is the
only eligible work of any kind for that domain, purely because ≥K other
domains rank better and stay non-empty. That side effect is reachable at
this crawler's real seed-domain count (§4.4) and gets worse, not better, as
link discovery runs. This was already flagged as needing a decision by
`frontier-optimization-audit.md` §4.6/§9 item 5 — this audit's contribution
is confirming it's real (not just "plausible") and precisely characterizing
when it resolves itself (finite backlogs) vs. when it doesn't (continuous
replenishment of ≥K better-ranked domains).

---

## 7. Local vs. Redis — behavioral differences

Both backends implement the **same** strict-priority-with-rate-limit-skip
policy and produced **identical** results on every finite scenario in this
audit (§4.1, 4.2, 4.3-partial, 4.5). The one deliberate difference is the
`K`-bounded visibility window (§1.2, §4.4), which exists only in the Redis
backend and is an explicit, documented tradeoff (bounded Lua worst-case
runtime) — not an accidental divergence. The local frontier pays for its
unbounded visibility with an unbounded-worst-case per-call scan of
`blocked_domains` instead; this is fine for the local frontier's real usage
(single in-process scheduler, not the K-bound's target problem of
many-simultaneous-network-round-trips) and was already accepted as correct
in the ADR.

---

## 8. Recommendation

```
CLARIFY PRIORITY POLICY BEFORE CHANGING CODE
```

More precisely, this audit's evidence supports two different answers for
two different questions bundled in the brief:

1. **Mechanism J (strict priority starving finite low-priority work under
   infinite high-priority replenishment): KEEP CURRENT SCHEDULER.** This is
   working as intentionally designed, confirmed against the ADR's own
   stated policy, and is standard, defensible behavior for a priority
   crawler (an operator who wants bulk/low-priority domains serviced under
   sustained high-priority load already has the lever that works today:
   set `rate_limit > 0`, which this audit measured to fully resolve the
   starvation in §4.3's third row). No code change is justified here
   without a product decision that strict priority is *not* what this
   crawler should do — that's a policy question for the user, not a bug
   this audit can "fix."
2. **Mechanism I (the `domain_scan_limit` visibility window, §4.4/§6):
   genuinely worth a real decision**, now backed by direct reproduction
   (not just the prior audit's reasoning) and a concrete production number
   (50 seed domains already at the default `K=50`). This audit takes no
   position on *which* of Strategies A-E (below) to apply, and does not
   implement any of them — per the brief's instruction, this needs a
   deliberate design decision weighed against the ADR's explicit K-bound
   rationale, not a quick patch.

No production code was changed as part of this investigation.

### Candidate strategies for mechanism I (not implemented, not chosen)

| Strategy | Correctness | Effect on existing priority semantics | Rate-limit interaction | Distributed safety | Redis/Lua complexity | Perf | Backward compat | Impl. complexity |
|---|---|---|---|---|---|---|---|---|
| **A. Aging** — effective priority improves with queue age | Sound if scored consistently; needs a second, time-dependent score term | Changes semantics: priority is no longer purely static/insertion-order | Orthogonal — aging and rate-gating can coexist | Needs the aging term computed server-side (Lua `TIME` call, already used) — safe | Moderate — `domain_heads` score becomes time-dependent, requires either periodic rescoring or an age-adjusted comparison at scan time | Rescoring cost if done eagerly; cheap if computed lazily at scan time | Existing `(priority, seq)` ordering changes for any domain that ages past a threshold — a real behavior change operators must be told about | Medium |
| **B. Bounded consecutive claims per domain** | Sound, simple to reason about | Weakens strict priority only when one domain would otherwise monopolize consecutive claims — doesn't touch cross-domain ordering when there's no monopoly | Independent of rate limiting | Needs a shared per-domain claim-streak counter in Redis (new key) | Low-moderate — one more counter read/write per claim in the Lua script | Negligible | Fully additive; default streak-limit = unlimited preserves current behavior exactly | Low-Medium |
| **C. Weighted fairness** (priority + fairness weight blended into one score) | Sound but the blend function itself becomes a new tunable with its own edge cases | Fundamentally changes what "priority" means — no longer a strict total order | Orthogonal | No new distributed-safety concern beyond the score formula itself | Moderate — new score formula touches `add_url`, `claim_next`, `reclaim_and_promote` all at once (three Lua scripts) | Negligible | Not backward compatible with any external assumption that priority is a strict order (nothing in this repo currently assumes that beyond the scheduler itself, per §1.1) | Medium-High |
| **D. Priority + starvation threshold** — promote once oldest-queued age exceeds a cap | Sound, and closest in spirit to "priority normally, fairness only as an escape valve" | Preserves strict-priority ordering exactly until the threshold fires, then only affects the specific starved domain | Independent | Needs each domain's oldest-queued-URL age visible to the scan (already derivable from its head's `seq`/insertion time via `meta:{url}.first_seen`) | Moderate — `claim_next` would need to special-case "promote a starved domain's head above its natural score" when scanning | Cheap if only evaluated for domains already outside the K window | Fully additive; threshold = infinity preserves current behavior exactly | Medium |
| **E. Priority-aware round-robin/heap among eligible domains** | Sound, most invasive | Replaces strict-priority-wins-always with priority-tier-then-round-robin — the largest semantic change of the five | Needs redesign of how rate-gated domains interact with round-robin position | Largest Lua/keyspace redesign of the five | High | Unclear without prototyping | Not backward compatible — this is a different scheduling policy, not a patch to the current one | High |

None of these was implemented. Strategy D is the least invasive to the
current, explicitly-chosen strict-priority policy (§6) while directly
targeting mechanism I's specific failure mode (a domain invisible past the
K window despite being otherwise fully eligible) — but that is an
observation for the next design conversation, not a recommendation to build
it now.

---

## 9. Next step

**STOP. Awaiting review before any further work.** The roadmap's next item
(SQLite batching / duplicate-write analysis) is not started as part of this
task.
