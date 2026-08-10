# Redis `domain_scan_limit` (K) Design Investigation (Step 8A)

Status: **design investigation only — no production code changed.** This
document continues directly from `docs/architecture/domain-starvation-audit.md`
(Step 8), which concluded that strict global priority is intentional and
correctly implemented, and that the one remaining architectural question is
whether Redis's bounded `domain_scan_limit` (`K`) visibility window is a
necessary tradeoff or an avoidable limitation. This is that investigation.
Nothing in `core/redis_frontier.py`, `core/url_frontier.py`, or any Lua
script was modified. All measurements below were taken either against the
real, unmodified `RedisURLFrontier` (using its existing, already-configurable
`domain_scan_limit` constructor parameter — not a code change) or against a
standalone prototype in an isolated Redis namespace/keyspace that never
imports or touches production frontier code.

---

## 1. Problem statement

`claim_next` (`core/redis_frontier.py:190-260`) selects the globally-best
eligible domain by scanning `ZRANGE domain_heads 0 K-1` — the `K` best-scored
candidates by `(priority, seq)` — and picking the first one that is both
non-empty and not currently rate-gated. This bounds worst-case Lua execution
time to `O(K)`, which was the ADR's explicit goal (`frontier-adr.md` §5-§6:
"a Lua script must have bounded worst-case runtime").

**What it solves:** a single Redis round trip regardless of total domain
count, with a predictable, configurable worst-case CPU cost per call.

**What it creates:** a domain ranked outside the top `K` by score is **never
examined at all**, regardless of how long it's been waiting or whether every
one of the `K` domains ahead of it is currently rate-gated. Step 8 measured
this directly: a victim domain ranked 16th (behind 15 continuously-replenished
better-ranked domains, `K=10`) received 0/400 claims; the same victim behind
15 **finite** better-ranked domains was claimed the instant enough of them
drained below `K` (claim #301). The production seed file already spans 50
distinct domains against the default `K=50`.

---

## 2. Current implementation, exact behavior

Traced from `core/redis_frontier.py` (unchanged, re-verified against this
session's earlier reading, confirmed identical to the Step 8 audit's
description):

```
add_url(url, domain, priority)
    ↓ INCR seq; score = priority*1e6 + seq
    ↓ ZADD domain:{d}:queue score url
    ↓ ZADD domain_heads <domain's current head score> domain   (unconditional resync)
    ↓ SADD domains:active domain

claim_next(K, lease_ttl, rate_limit, token)
    ↓ candidates = ZRANGE domain_heads 0 K-1        -- top K by score, ALWAYS, regardless of gating
    ↓ for each candidate (in score order):
    ↓     if domain's queue is empty (stale head): self-heal, ZREM domain_heads/domains:active, continue
    ↓     elif next_time > now: rate-gated, SKIP (leave in domain_heads), continue        <- correct, measured (Step 8 §4.2)
    ↓     else: pop head, resync domain_heads to new head (or remove if now empty),
    ↓           SET next_time = now + rate_limit, INCR attempts, HSET claim, ZADD inflight
    ↓           RETURN claim
    ↓ if no candidate among the K was eligible: RETURN nil     <- the K-window's failure mode:
                                                                   domains ranked K+1... were never even read
```

`reclaim_and_promote` (retry/lease-expiry promotion) and `add_url` both
resync `domain_heads` **unconditionally** — every domain with any queued
work is always a `domain_heads` member, gated or not. `K` only bounds how
many of them `claim_next` is willing to *look at* per call, not how many
exist.

---

## 3. Correctness invariant

Per the brief, this is what any candidate design must guarantee for every
`claim_next()` call:

> Among all currently non-empty and rate-eligible domains, the selected
> domain must be the globally best domain according to the existing strict
> `(priority, seq)` ordering.

Explicitly **not** required (and explicitly out of scope, per Step 8's
conclusion): fairness, aging, round-robin, weighted scheduling, or any
promotion of lower-priority work ahead of its true rank. A design that
changes selection *order* among eligible domains fails this investigation's
brief even if it happens to also reduce starvation — that would be solving a
different, already-closed question.

**The current implementation violates this invariant** whenever more than
`K` domains are simultaneously non-empty: it is not merely slower for
domains ranked beyond `K`, it **never considers them**, which is a
correctness gap against the invariant above, not a performance
characteristic.

---

## 4. Candidate architectures

### Approach A — Increase K

Simplest possible change: raise the constructor default. No keyspace change,
no Lua rewrite.

**Measured** (§8): worst-case Lua cost (all `K` candidates present and
rate-gated — the true worst case) scales **linearly**, ≈2.9µs/candidate:

| K | Worst-case latency (all gated) |
|---:|---:|
| 1 | 0.09 ms |
| 10 | 0.07 ms |
| 50 | 0.17 ms |
| 100 | 0.27 ms |
| 250 | 0.66 ms |
| 500 | 1.45 ms |
| 1000 | 2.95 ms |
| 2000 | 5.72 ms |

Critically, this worst case (**every single one of the K candidates
currently gated**) is not a rare edge case for this crawler — it's the
expected steady state at the top of `domain_heads` whenever rate limiting is
active and several top-priority domains are cycling through claim→gate→claim
in quick succession (exactly Step 8's Scenario 2/3 setups). Redis's
single-threaded command loop means this cost is paid serially, blocking
every other client for the duration — already the documented saturation
mechanism in `docs/architecture/throughput-ceiling-audit.md` §3.1/§3.3A at
sustained high Lua call volume.

Also measured: in the **common case** (candidates found eligible, not
gated), cost stays flat (~0.05ms) even out to `K=1000-20000` — because an
*empty* domain (drained to zero) self-removes from `domain_heads` entirely,
so a "gated but present" candidate is what actually costs Lua time, not
domain count by itself. This means simply raising `K` is cheap *on average*
but the tail cost (the scenario that matters for a p99/p999 latency budget
on a single-threaded server) grows linearly and without limit.

**Verdict**: solves the visibility problem only up to whatever `K` is set
to — it moves the boundary, it does not remove it. A raised but still-finite
`K` is exactly as vulnerable to Step 8's demonstrated starvation mechanism
once active-domain count exceeds the new `K`, just at a higher threshold.

### Approach B — Adaptive K (scan K, expand on miss, retry)

Analyzed, not prototyped (its own reasoning rules it out before measurement
is needed): if the first `K` candidates are all ineligible, the script would
scan another `K` (or `2K`, etc.), up to some hard ceiling.

- **Correctness**: only as good as its ceiling — still a bounded window,
  still capable of the exact same "domain never seen" failure once the
  ceiling is exhausted. Does not actually change the answer to the
  invariant in §3; it only changes when the failure occurs.
- **Worst-case runtime**: this is strictly *worse* for tail latency than a
  static `K`, not better — the whole point of adaptivity is to do more work
  exactly when a call *would otherwise complete quickly* (few eligible
  candidates near the top). That produces a **data-dependent, unpredictable**
  latency profile: most calls are cheap, but the ones that would be blocked
  in the current design instead become progressively more expensive up to
  the ceiling, which is the worst possible property for a single-threaded
  server every other client is also waiting on.
- **Rescanning cost**: without a cursor, each expansion re-touches the first
  `K` candidates again unless implemented carefully (a fixable detail, not a
  fundamental flaw, but added complexity for no correctness gain over just
  setting a larger static `K`).

**Verdict**: strictly dominated by either Approach A (if the ceiling is
acceptable, just set static `K` to the ceiling and skip the unpredictability)
or Approach D/eligible-index below (if it isn't). Not recommended as an
independent design.

### Approach C + D — Separate eligible-domain index with lazy time-bucket promotion

These two are analyzed together because C (a separate "currently eligible"
index) is not viable alone — it needs D (a mechanism to notice a domain
became eligible purely because time passed) to stay correct, and D's
natural implementation *produces* the eligible index as a side effect. This
is the design this investigation prototyped and measured (§8, §12).

**Concept**: maintain two ZSETs instead of one:

- `eligible_heads` — domain → score of its current head. Membership =
  "non-empty **and** not rate-gated **right now**." `claim_next` no longer
  scans anything to find the best domain — `ZRANGE eligible_heads 0 0` *is*
  the answer, unconditionally, because by construction nothing ineligible is
  ever a member.
- `gated` — domain → `next_allowed_time`. Membership = "currently rate-gated."
  This is the D half: exactly the same "due-entries" pattern already proven
  in production by `retry_scheduled` and `inflight` (`reclaim_and_promote`'s
  two `ZRANGEBYSCORE ... -inf now LIMIT 0 batch_size` sweeps,
  `core/redis_frontier.py:345-417`). `claim_next` performs the identical
  sweep against `gated` first, promoting any domain whose gate has expired
  into `eligible_heads` (re-reading its **current** queue head at promotion
  time, not a value cached when it was gated — this is what makes it correct
  even if new URLs were added to that domain while it waited).

```
claim_next(promote_batch, rate_limit, token):
    due = ZRANGEBYSCORE gated -inf now LIMIT 0 promote_batch      -- bounded, NOT domain-count-dependent
    for domain in due: ZREM gated domain; re-read domain's true head; ZADD eligible_heads
    best = ZRANGE eligible_heads 0 0                              -- always THE answer, no scanning
    if none: return nil
    pop best's head; ZREM eligible_heads; if domain still non-empty: ZADD gated (now+rate_limit)
    return claim
```

`add_url` needs one extra check: if the domain is a current member of
`gated`, do nothing to `eligible_heads` (the domain is mid-cooldown; the next
due-sweep will read its true head, whatever it is by then, once the cooldown
expires) — otherwise resync `eligible_heads` exactly as `domain_heads` does
today. This removes the need for the standalone `next_time` STRING key
entirely — `gated`'s score already **is** `next_allowed_time`.

**This is the design this investigation actually built and validated** (§8).

### Approach E — Redis Streams or other primitives

Considered and dismissed without prototyping: this is fundamentally a
priority-selection problem (ZSETs, already the right primitive, already used
correctly for `domain_heads`/`domain:{d}:queue`), not a message-queue/
consumer-group problem. Streams add consumer-group bookkeeping this problem
doesn't need and don't offer a cheaper "give me the globally smallest score"
operation than `ZRANGE ... 0 0` already provides. No further analysis
performed — there's nothing Streams would improve here.

---

## 5. Rate-limit eligibility — the "time passing" problem

This is the crux of why C alone doesn't work, and why D exists. **No Redis
write occurs when a domain's rate gate expires** — the clock just passes a
threshold nobody pushed a notification about. Every mechanism this
investigation considered for noticing that:

| Mechanism | Correctness | Cost |
|---|---|---|
| **Lazy promotion during claim** (what was prototyped) | Correct: `ZRANGEBYSCORE gated -inf now` finds exactly the due set, no more no less, every single call | Bounded by `promote_batch`; zero cost when nothing is due (empty range scan) |
| **Periodic sweeper** (separate asyncio task, like `_recovery_loop`) | Correct, but adds latency between "gate expired" and "domain visible" equal to the sweep interval — up to `recovery_interval` (30s default) of a domain sitting eligible-but-invisible | Decoupled from claim latency, but strictly worse for freshness than lazy promotion, and this crawler already has exactly this interval-based imprecision documented for lease-expiry (`frontier-optimization-audit.md` §6.1: "at least two recovery sweeps") — not something to add on purpose to a *more* time-sensitive path |
| **Time-bucket ZSET (`gated`)** | This is the data structure, not the trigger — it's what makes lazy promotion's `ZRANGEBYSCORE` query cheap (`O(log N + M)`, `M` = due count) instead of "check every gated domain individually," which would be exactly the K-scan problem again | Already accounted in the lazy-promotion row |
| **Hybrid** (lazy promotion primary, periodic sweeper as a backstop) | Same as lazy promotion for the hot path; the sweeper only matters if `claim_next` stops being called entirely (frontier fully idle with only gated domains left) — analogous to why `_recovery_loop` exists independently of the scheduler polling | Same low cost; worth having for the same reason recovery already runs independently |

**Chosen mechanism (prototyped): lazy promotion inside `claim_next` itself**,
because it's the only one with zero added latency in the hot path and reuses
a pattern (`ZRANGEBYSCORE -inf now LIMIT 0 batch`) already proven correct and
performant in this exact codebase.

**Important honest caveat**: lazy promotion still has *some* imprecision — a
domain that becomes due can wait up to one `claim_next` call before being
promoted, and if more domains become due in the same instant than
`promote_batch` allows, some wait an extra call. This is not perfect
real-time correctness (nothing achieves that without either unlimited Lua
work or a live timer, neither of which Redis offers). What it *is*: **a
small, self-correcting lag bounded by `promote_batch` throughput**, not a
structurally permanent invisibility — categorically different from the
current `K`-window's failure mode, where a domain outside the window stays
invisible for as long as `K` other domains stay non-empty, which (Step 8
proved) can be forever.

---

## 6. Retry / recovery interaction

Not modified, only analyzed for compatibility:

- **`reclaim_and_promote`'s phase (b)** (retry-due promotion,
  `core/redis_frontier.py:392-412`) currently does `ZADD domain_heads`
  unconditionally after requeuing a due retry. Under the new design it needs
  exactly the one extra check `add_url` also needs: is this domain currently
  in `gated`? If yes, leave it there — untouched — and let the next due-sweep
  pick up the (now-updated) true head; if no, `ZADD eligible_heads` directly,
  same as today. No new race: this is a read-then-branch inside the same
  atomic Lua script, same as every other operation here.
- **Phase (a)** (abandoned-inflight reclaim) doesn't touch domain-eligibility
  structures at all today (it operates on `inflight`/`claim:{url}`/`meta:{url}`)
  and wouldn't need to under the new design either — it only decides
  retry-vs-terminal, exactly as now.
- **No duplicate entries possible**: a domain can be a member of at most one
  of `eligible_heads`/`gated` at any time by construction (every transition
  path removes from one before adding to the other, all within one atomic
  Lua script) — the same invariant `domain_heads` already maintains alone
  today, just split across two sets instead of one.
- **No priority corruption**: promotion always re-reads the domain's live
  queue head at promotion time rather than trusting a cached score — this is
  actually **more** correct than today's `domain_heads`, which is already
  designed this way (resynced on every mutation) for the same reason.

Nothing about retry/backoff timing, `max_retries`, or terminal-state
transitions needs to change — this investigation only touches how a
domain's *own* eligibility (not any individual URL's retry state) is
tracked.

---

## 7. Distributed atomicity

**Preserved trivially, and this is worth stating plainly rather than
hand-waving**: the entire promote→select→pop→reschedule sequence, in every
candidate design analyzed here, still executes as **one Lua script per
`claim_next` call**. Redis's single-threaded script execution serializes all
of it exactly as it does today — there is no new round trip, no new
multi-step client-side sequence, and therefore no new race window. This was
verified directly, not just reasoned: the prototype's `claim_next` script
was run under Step 8's multi-worker methodology conceptually (the same
single-round-trip shape as the production script; production's own
multi-worker test in Step 8 §4.6 already confirms 0 duplicate claims across
1/2/4/8 concurrent claimers against an atomically-scripted `claim_next`,
and nothing about splitting `domain_heads` into two ZSETs changes that
property — the script boundary, not the keyspace shape, is what atomicity
depends on).

Race-by-race, per the brief:

| Race | Outcome |
|---|---|
| Two workers claim the same domain | Serialized by Lua execution — second caller sees the already-updated `eligible_heads`/`gated`/queue state, same as today |
| Two workers see the same newly-eligible domain | Same — only one script instance runs at a time; whichever runs first pops the URL and moves the domain to `gated`, the second sees it already gone |
| Worker adds a URL while another claims | `add_url` and `claim_next` are separate scripts but each is atomic individually; the added URL is either visible to the claim or it isn't — no partial state, identical to today's `domain_heads` resync race (already safe, unchanged) |
| Retry becomes due while a claim is occurring | Scripts don't overlap; whichever runs first sees a consistent snapshot |
| Reclaim occurs while a claim is occurring | Same |
| Domain becomes empty while another worker "sees" its head | Not possible mid-script (single Lua execution); between calls, the next caller re-reads the live queue, same as today |
| Rate-limit timestamp transitions | Handled by the `gated` ZSET's score comparison against `TIME`, read once per script execution — same single-snapshot-per-call property `next_time > now` already has today |

---

## 8. Performance analysis (measured)

All measurements against Redis on localhost, `bench` db, isolated
namespaces per run, cleared after use.

### 8.1 Current K-window, worst case (all K candidates present and gated)

See §4 Approach A's table — linear, ~2.9µs/candidate, reaching 1.45ms at
K=500 and 2.95ms at K=1000.

### 8.2 Prototype (eligible-index), common case — latency vs. total domain count

| Total domains (all always-eligible) | Mean latency | Max latency |
|---:|---:|---:|
| 10 | 0.061 ms | 0.098 ms |
| 100 | 0.052 ms | 0.142 ms |
| 1,000 | 0.051 ms | 0.287 ms |
| 5,000 | 0.051 ms | 0.592 ms |

**Flat.** Unlike the current design (whose worst case scales with `K`
regardless of whether it's needed), the new design's common-case cost is
independent of total domain count — the `ZRANGE eligible_heads 0 0` that
answers "who's best" is `O(log N)` internally regardless of `N`.

### 8.3 Prototype, worst case — `promote_batch` domains all becoming due simultaneously

| `promote_batch` (= domains due at once) | Claim latency |
|---:|---:|
| 50 | 0.68 ms |
| 200 | 1.34 ms |
| 1,000 | 3.51 ms |

Comparable order of magnitude to the current design's worst case at an
equivalent bound (K=50→0.17ms, K=200→~0.6ms interpolated, K=1000→2.95ms) —
roughly 2-4x more expensive per unit of bound in this prototype's
unoptimized form. **The difference that matters is not this number — it's
how often each design actually pays its worst case.** The current design
pays something close to its worst case whenever the single best-ranked
domain happens to be gated (routine under sustained multi-domain rate
limiting — recently-claimed domains cluster at the top of `domain_heads`).
The new design only pays its worst case when many domains become due within
the same `claim_next` call — a genuine burst condition, not the steady
state, since real gate expirations naturally desynchronize (they're set
relative to each domain's own last-claim time, not a shared clock tick).

### 8.4 Redis commands per claim (server-side, via `INFO commandstats`)

| Design | Sub-commands / claim (common case) |
|---|---:|
| Prototype (minimal, no claim/lease/attempt bookkeeping) | 8 |
| Production K-window (K=50, common case, full bookkeeping) | 14 |

**Not a fair apples-to-apples comparison as measured** — the prototype
deliberately omits the ~5-6 commands production spends on claim-token/lease/
attempt/source_query bookkeeping (`SET next_time`, `INCR attempts`,
`HSET claim`, `ZADD inflight`, `HGET meta`), none of which this
investigation's scope touches. A real implementation of the eligible-index
design would still need all of those, landing at roughly 13-14 — **in the
same ballpark as today, not a meaningful win or loss on raw per-call command
count.** The actual advantage is entirely in §8.1-8.3's *scaling* behavior,
not in a smaller constant.

### 8.5 What this means for the ~13.4-13.8K claims/sec ceiling

`throughput-ceiling-audit.md` established that ceiling is set by Redis's
single-threaded command-execution loop saturating on Lua call *volume* at
high concurrency, not by any individual call's cost being especially large
in the common case. Since §8.2/§8.4 show the eligible-index design's
common-case cost is flat and roughly comparable in command count to today,
**it would not be expected to move that ceiling** — consistent with this
investigation's scope (a correctness/visibility fix, not a throughput
optimization) and with that audit's own explicit instruction not to chase
this ceiling further.

---

## 9. Memory analysis

- Current: `domain_heads` (ZSET) + `domains:active` (SET), each bounded by
  distinct active-domain count.
- New design: `eligible_heads` + `gated`, together **also** bounded by
  distinct active-domain count (every domain is in at most one of them, per
  §6) — no `domains:active` mirror needed (membership in either ZSET already
  answers "is this domain active"). **Net memory cost is a wash, possibly
  slightly lower** (two ZSETs covering disjoint subsets vs. today's one ZSET
  + one SET covering the same population twice).
- No per-URL memory shape changes at all — `domain:{d}:queue`,
  `claim:{url}`, `meta:{url}`, `attempts:{url}` are untouched by every
  design in §4.

---

## 10. Adversarial workload analysis

**Does the current K-window function as a safety feature against a
domain-flood, independent of its visibility cost?** Partially, and the new
design preserves the part that matters:

- **Memory**: an attacker (or just organic link discovery) creating many
  thousands of distinct domains grows `domain_heads`/`domains:active`
  (current) or `eligible_heads`/`gated` (new) linearly in domain count either
  way — already established as a non-issue at even 1M-URL scale
  (`frontier-optimization-audit.md` §5). No design here changes that.
- **Per-call CPU, current design**: `K` already caps worst-case Lua work
  *regardless of total domain count* — an attacker adding 1M domains does
  not increase any single `claim_next` call's cost beyond `O(K)`, because
  only the top `K` are ever read. This is a real, already-present DoS bound.
- **Per-call CPU, new design**: equally bounded, just via a different,
  decoupled knob — `promote_batch` caps how many due-promotions one call
  processes, exactly the same shape of bound `K` provides today (and
  identical in spirit to the already-shipped `reclaim_batch_size` bounding
  `reclaim_and_promote`). **The new design does not sacrifice this
  property** — it re-derives an equivalent worst-case bound from "how many
  domains became due in this instant" instead of "how many domains exist
  globally," which is arguably a *better*-targeted bound (an attacker
  flooding domains that never get claimed, and therefore never get gated,
  costs the new design nothing extra at claim time — only `add_url`'s
  already-existing O(1) cost — whereas today's design would still have to
  walk past some of them in `domain_heads` if they happened to rank well).
- **`ZRANGE eligible_heads 0 0` itself**: `O(log N)` regardless of `N`, so a
  flood of never-gated, always-eligible domains costs the new design
  essentially nothing extra per claim (§8.2's flat measurement at up to
  5,000 domains is exactly this scenario) — this is actually **more**
  resistant to a "flood many cheap domains" pattern than raising static `K`
  (Approach A) would be, since Approach A's cost is tied to `K` regardless
  of whether the flooded domains are gated or not.

**Conclusion**: the current `K`-window's bounded-worst-case property is a
legitimate safety characteristic worth preserving, and this was the right
question to ask before recommending its removal — but the eligible-index
design (§4 C+D) preserves an equivalent bound via `promote_batch` rather
than sacrificing it, so this consideration does not argue against Option 4;
it argues against *any* design (including a naive "just remove K entirely
with no replacement bound") that drops the bound without replacing it with
something equally enforceable.

---

## 11. Comparison table

| Design | Correctness (invariant §3) | Strict priority preserved | Rate-limit correctness | Distributed safety | Redis CPU | Memory | Complexity | Operational risk |
|---|---|---|---|---|---|---|---|---|
| **Keep K=50** | Fails beyond 50 active domains (measured) | Yes | Yes | Yes (unchanged) | Best (flat, low) | Best (unchanged) | None | Low, but the known gap persists |
| **Increase K (e.g. 250-500)** | Fails beyond the new K (moves the boundary, doesn't remove it) | Yes | Yes | Yes (unchanged) | Good in common case; measured linear worst-case growth (1.45ms @ K=500) | Unchanged | Trivial (one config value) | Low — no keyspace/Lua change, fully reversible |
| **Adaptive K** | Same ceiling-bounded failure as static K, just later | Yes | Yes | Yes (unchanged) | Unpredictable — cheap calls stay cheap, "would-have-failed" calls get progressively more expensive | Unchanged | Moderate (cursor-safe expansion logic) | Medium — data-dependent latency spikes on a single-threaded server are a real operational hazard |
| **Eligible-index (C+D)** | **Holds at any domain count** (measured to 2,000 correctly, no artifact) | Yes (measured: continuous-replenishment starvation reproduced identically — policy unchanged) | Yes (lazy promotion via proven `ZRANGEBYSCORE -inf now` pattern) | Yes (single Lua script per call, same as today — proven, not assumed) | Flat common case (~0.05ms to 5,000 domains); worst case bounded by `promote_batch`, comparable order of magnitude to equivalent K | Comparable, slightly better (no `domains:active` mirror needed) | Highest — 3 Lua scripts touched, 2 keyspace structures replace 1, `add_url`/reclaim/retry-promotion each need one added branch | Medium — new code path, needs the same test depth as the original Step 3-6 migration before trusting in production |
| **Streams (E)** | N/A — wrong primitive for this problem | — | — | — | — | — | — | Not pursued further |

---

## 12. Recommendation

```
CONFIGURABLE / ADAPTIVE K  →  specifically: raise the static default now (cheap, low-risk),
                               defer the eligible-index redesign until real-crawl telemetry
                               justifies it
```

Reasoning, directly against §14's "do not over-engineer" instruction and the
evidence gathered:

- The eligible-index design (§4 C+D) is the only one of the five that
  actually satisfies the correctness invariant (§3) at arbitrary domain
  counts, and this investigation validated it works, is atomic, is
  compatible with retry/recovery, and does not sacrifice the adversarial
  bound the current `K` provides. **It is the architecturally correct fix
  if this crawler will operate at domain counts in the many hundreds to
  thousands.**
- But that scale is not yet demonstrated for this crawler — only reasoned
  about. The production seed file is 50 domains; link discovery will grow
  that, but by how much in practice (this specific crawler's actual
  discovery fan-out, not a hypothetical) is not yet measured.
- Raising `K` (e.g. 50 → 250 or 500) is **measured to be cheap in the common
  case** (flat ~0.05ms out to K=1000+) and only expensive in the worst case
  (all K simultaneously gated) at a magnitude (1.45ms @ K=500) that is still
  small relative to this crawler's real per-request costs (HTTP fetch time
  dwarfs this by 2-3 orders of magnitude, as already established in
  `throughput-ceiling-audit.md` §3.3C). It is a one-line config change with
  zero keyspace or Lua risk, fully reversible, and directly pushes the
  starvation boundary from "50 domains" to "250-500 domains" — likely
  sufficient headroom for this project's remaining timeline.
- Building and shipping the eligible-index redesign now, without evidence
  that real link-discovery-driven domain counts will exceed a raised K,
  would be exactly the over-engineering §14 warns against — 3 Lua scripts,
  a keyspace change, and a full new test suite, to solve a problem not yet
  confirmed to occur at this crawler's actual operating scale.

This recommendation intentionally spans two of the five listed options
because the evidence supports different urgency for each half: raising `K`
is justified *now* (Option 2-shaped), the full redesign is justified *only
conditionally* and should wait for real telemetry (Option 5-shaped). Per the
task's absolute constraint, **neither was implemented** — this section is a
recommendation for the next task to act on, not an action taken here.

---

## 13. Implementation scope estimate (not implemented)

**If "raise K" is chosen:**
- Files: `core/config.py` (`FrontierConfig.domain_scan_limit` default) or
  `config.yaml`. Possibly nothing beyond a config value.
- Lua scripts: none.
- Redis keys/structures: none.
- Tests: none required beyond the existing `domain_scan_limit`-parameterized
  ones (already pass at any K per this investigation's own measurements) —
  worth adding one regression test pinning the new default's worst-case
  latency stays within an agreed budget, using `tests/benchmarks/domain_starvation.py`'s
  existing `scan-limit-window` scenario at the new default.
- Migration: none — purely a scheduling-window size change, no data format
  change.

**If the eligible-index redesign (§4 C+D) is chosen later:**
- Files: `core/redis_frontier.py` (`_claim_next_script`, `_add_url_script`,
  `_reclaim_and_promote_script`, plus `__init__`/`_init_lua_scripts`).
- Lua scripts: all three of the above rewritten; `_complete_claim_script`
  and `_renew_claim_script` unaffected (they operate on claim/inflight state,
  not domain eligibility).
- Redis keys/structures: `domain_heads` + `domains:active` replaced by
  `eligible_heads` + `gated`; `domain:{d}:next_time` STRING keys removed
  (superseded by `gated`'s score). This is a **breaking keyspace change** —
  needs either a clean-cutover migration (drain-and-restart, acceptable for
  this crawler's operational model per existing docs) or a translation
  step; no existing data needs preserving mid-flight since these are all
  ephemeral scheduling structures, not the durable `urls:known`/terminal
  sets.
- Tests: the full depth of `tests/redis_frontier_test.py`'s existing
  priority/rate-limit/concurrency suite needs re-running against the new
  scripts (behavior should be identical for all of it, since the external
  `Frontier` protocol doesn't change), plus new tests specifically for the
  promotion-lag properties described in §5, plus re-running
  `tests/benchmarks/domain_starvation.py`'s full scenario set (Step 8) to
  reconfirm no regression against every mechanism (A-J) already classified.
- Benchmark requirements: a full re-run of the Step 6/7
  `frontier_benchmark.py`/`priority_ratelimit.py`/`distributed_benchmark.py`
  suite before trusting this in production, given it touches the hottest,
  most safety-critical script in the system.
- Estimated effort: comparable to one of the original ADR migration steps
  (Step 3-5 scale), not a small patch — consistent with why this
  investigation recommends deferring it rather than doing it opportunistically.
