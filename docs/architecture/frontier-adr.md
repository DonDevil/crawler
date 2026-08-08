# ADR: Frontier Contract + Redis Frontier v2 (Revision 2)

Status: design approved with changes below, **implementation not started**. This revision incorporates: failure classification (Decision 1), asyncio-background lease recovery (Decision 2), `domain_heads`-based global priority (Decision 3), typed `FrontierClaim` tokens, a renewal recommendation, and an explicit metadata lifecycle.

---

## 0. Existing failure paths (survey, all 6 crawler backends)

Read `crawler/{async,http,tor,playwright,selenium,scrapling,hybrid}_crawler.py`. All 6 share one shape, with no common base class — each reimplements it independently:

- `fetch()` has its **own internal retry loop**, bounded by a per-backend `max_retries` (2–3) and per-attempt `timeout` (20–30s). It retries transport-level failures (5xx, exceptions) and gives up early on non-retryable ones (4xx). Returns `(html | None, failure_reason | None)`. This loop is a transport concern and is **out of scope** — it stays as-is.
- `worker()` then does, identically in every file:
  ```python
  if URLUtils.is_blacklisted(url):
      self.frontier.mark_visited(url)          # → should be mark_skipped
      ...
      continue
  html, failure_reason = await self.fetch(...)
  status = "visited"
  ...
  elif failure_reason:
      status = "failed"
      ...
  self.frontier.mark_visited(url)               # ← called unconditionally, even on failure
  ```
- The outer `except Exception as exc: logger.error(...)` catch-all (parser errors, media DB errors, anything `fetch()` didn't already catch) makes **no frontier call at all** — today the URL is silently dropped from the frontier's bookkeeping; under the new claim model this is exactly the case lease-recovery exists for, but it should still fail fast rather than wait out a full lease TTL.

**Design implication — avoid duplicating retry logic 6×:** the *decision* of "is this failure retryable, and how many times has this URL failed" must live in exactly one place: the frontier's completion path (`mark_failed`), keyed off the claim's `attempt` number and a single `max_retries` config value. Crawler backends do not classify failures — they just report "it succeeded" / "it failed, here's why" / "skip it," and the frontier decides retry vs. terminal. That collapses the required per-backend change to a small, mechanical, identical edit in 6 files (see §11), plus one shared helper used by all of them for the heartbeat-wrapped fetch call (§8).

---

## 1. Final frontier contract

```python
@dataclass(frozen=True)
class FrontierClaim:
    url: str
    token: str            # opaque unique id for this specific claim (uuid4 hex)
    attempt: int           # 1-based: which attempt this is for this URL
    domain: str
    priority: int
    lease_expires_at: float  # epoch seconds; informational, renewed via renew_claim
    source_query: str = ""
```

```python
class Frontier(Protocol):
    def add_url(self, url: str, priority: int = 10, source_query: str = "") -> bool: ...
    def get_next_url(self) -> FrontierClaim | None: ...
    def renew_claim(self, claim: FrontierClaim) -> FrontierClaim | None: ...
    def mark_visited(self, claim: FrontierClaim) -> None: ...
    def mark_failed(self, claim: FrontierClaim, error: str = "") -> None: ...
    def mark_skipped(self, claim: FrontierClaim) -> None: ...
    def has_pending(self) -> bool: ...
    def pending_count(self) -> int: ...
    def get_source_query(self, url: str) -> str: ...
    def get_status_counts(self) -> dict[str, int]: ...
    def clear(self) -> None: ...
    def close(self) -> None: ...
```

Behavioral guarantee per method (this is the part that matters, not the signature):

| Method | Guarantee |
|---|---|
| `add_url` | Returns `True` iff the URL was not already known (queued, in-flight, retry-pending, or terminal). Dedup is monotonic — once known, never re-added by this path. Idempotent no-op otherwise. |
| `get_next_url` | Atomically removes exactly one URL from exactly one domain's ready queue and hands the caller sole ownership via `claim.token`, respecting global priority and per-domain rate limits. Returns `None` only when no eligible work exists *right now* (may still exist but rate-gated). |
| `renew_claim` | Extends the lease iff `claim.token` is still the current owner of that URL's claim. Returns an updated `FrontierClaim` with a new `lease_expires_at` on success, `None` if the claim was already reclaimed (caller must abandon work — its result is no longer authoritative). |
| `mark_visited` | Terminal success, iff `claim.token` matches. Stale tokens are silently ignored (logged), never applied. Idempotent. |
| `mark_failed` | Validates token, then centrally decides retry-vs-terminal from `claim.attempt` vs. configured `max_retries`: retryable → scheduled for requeue after backoff; exhausted → terminal `failed_permanent`. |
| `mark_skipped` | Terminal, no retry, ever (blacklist, robots, etc.), iff token matches. |
| `has_pending` | `True` iff there is queued, in-flight, or retry-pending work — i.e. the crawl isn't done yet. |
| `pending_count` | Count of URLs not yet in a terminal state (queued + retry-pending; in-flight is reported separately in `get_status_counts`, see below — kept out of `pending_count` since it's already "being worked," matching today's semantics where dequeued-but-unfinished work doesn't inflate `pending_count`). |
| `get_source_query` | Best-effort lookup, empty string if unknown/expired. |
| `get_status_counts` | `{queued, inflight, retry_scheduled, visited, skipped, failed_permanent}` — every URL the frontier has ever seen is in exactly one bucket at any instant (mirrors §2's state machine). |
| `clear` | Wipes all frontier state. Testing/reset only. |
| `close` | Releases connections/resources. No guarantee about in-flight claims (callers should drain first). |

No methods added beyond what's needed to make retry, recovery, and claim-safety real — nothing here exists "for symmetry" alone.

---

## 2. URL state machine

```
                 add_url()
                    │
                    ▼
               ┌─────────┐
      ┌───────▶│ QUEUED  │◀────────────┐
      │        └────┬────┘             │
      │             │ get_next_url()    │ backoff expired /
      │             ▼                   │ lease expired & attempt < max
      │        ┌─────────┐             │
      │        │INFLIGHT │─────────────┘
      │        └────┬────┘
      │             │
      │   ┌─────────┼─────────────┐
      │   │         │             │
      │ mark_       │        mark_failed()
      │ visited()   │        (attempt < max_retries)
      │   │         │             │
      │   ▼         │             ▼
      │┌────────┐   │      ┌───────────────┐
      │VISITED  │   │      │RETRY_SCHEDULED│──(backoff timer expires)──▶ QUEUED
      │(terminal)│   │      └───────────────┘
      │└────────┘   │
      │             │ mark_skipped()
      │             ▼
      │        ┌─────────┐
      │        │SKIPPED  │ (terminal)
      │        └─────────┘
      │
      │  mark_failed() with attempt >= max_retries,
      │  OR lease-reclaim with attempt >= max_retries
      └──────────────────────────────▶ ┌────────────────┐
                                        │FAILED_PERMANENT│ (terminal)
                                        └────────────────┘
```

Every URL is in exactly one of `{QUEUED, INFLIGHT, RETRY_SCHEDULED, VISITED, SKIPPED, FAILED_PERMANENT}` at any instant — this invariant is what makes the dedup/race problems from Revision 1 go away (§4 of the old doc, problems #1–#2): there is no window where a URL is in none of these.

---

## 3. Claim / lease lifecycle

1. **Claim.** `get_next_url()` atomically: picks the globally-best eligible domain (via `domain_heads`, §6), pops that domain's head URL, generates `token = uuid4().hex`, increments that URL's durable attempt counter, records `{token, attempt, domain, priority}` as the *current claim* for that URL, and puts the URL in the in-flight set scored by `now + lease_ttl`. Returns a `FrontierClaim`.
2. **Ownership.** The claim's `token` is the sole proof of ownership. Any completion or renewal call must present it.
3. **Renewal (optional, recommended — see §8).** `renew_claim(claim)` re-validates the token and bumps the lease. If another claim has since replaced this one (because this claim's lease already expired and was reclaimed), renewal fails and returns `None` — the caller must stop processing, its eventual `mark_visited`/`mark_failed` would be rejected anyway.
4. **Completion.** Exactly one of `mark_visited` / `mark_failed` / `mark_skipped` is called with the claim. The implementation validates `token` against the currently-stored claim record; on mismatch, the call is a no-op (logged as "stale claim ignored" — this is the mechanism that prevents a slow, since-reclaimed worker from corrupting a newer worker's claim on the same URL, the exact race called out in the requirements). On match, the claim record and in-flight entry are removed and the URL transitions per §2.
5. **Recovery.** A background task (§7) periodically finds in-flight entries whose lease has expired and whose token was *never invalidated by a completion call* — i.e., genuinely abandoned — and requeues or terminalizes them using the same attempt-vs-max_retries decision `mark_failed` uses.

---

## 4. Failure / retry semantics

- **Two independent retry layers, not to be conflated:**
  - *Transport-level* (existing, per-backend, inside `fetch()`): retries within a single claim/attempt, bounded by each backend's own `max_retries`/`timeout`. Unchanged.
  - *Frontier-level* (new): retries **across separate claims**, i.e. across separate `get_next_url()` calls for the same URL, bounded by one shared `frontier.max_retries` config value, applied uniformly by whichever frontier backend is active.
- `mark_failed(claim, error)`: reads `claim.attempt`; if `< max_retries`, computes `backoff = min(base_backoff * 2**(attempt-1), max_backoff)` and schedules the URL for requeue at `now + backoff` (`RETRY_SCHEDULED`); if `>= max_retries`, moves it straight to `FAILED_PERMANENT`. No unlimited retries — this is the only place the limit is enforced, so it can't drift between backends.
- Lease-expiry reclamation (§7) uses the *identical* attempt-vs-`max_retries` decision — a crashed worker's abandoned claim is treated exactly like an explicit failure, just without an error string.
- `mark_skipped` never retries, regardless of attempt count — used for blacklist/robots-style intentional exclusions, matching today's blacklist behavior in `URLFrontier.get_next_url` and the six crawler backends' blacklist branch.

Config additions (`FrontierConfig`):
```python
lease_ttl: float = 90.0          # seconds; see §8 for why 90s is reasonable with renewal
max_retries: int = 3
base_backoff: float = 5.0
max_backoff: float = 300.0
recovery_interval: float = 30.0  # seconds between background reclaim sweeps
reclaim_batch_size: int = 200
```

---

## 5. Redis keyspace (final)

| Key | Type | Purpose | Lifecycle |
|---|---|---|---|
| `crawler:{ns}:seq` | STRING (INCR) | Global monotonic sequence for stable priority ordering | Never cleared except `clear()` |
| `crawler:{ns}:urls:known` | SET | Permanent dedup memory — every URL ever accepted by `add_url` | Added once, never removed (except `clear()`). Replaces the old separate `queued`/`visited` dedup checks; closes the double-add race from Revision 1 §1.2, since a URL stays "known" through queued→inflight→retry→terminal with no gap. |
| `crawler:{ns}:urls:visited` | SET | Terminal-success membership, for `get_status_counts`/audits | Added on `mark_visited`, permanent |
| `crawler:{ns}:urls:skipped` | SET | Terminal-skip membership | Added on `mark_skipped`, permanent |
| `crawler:{ns}:urls:failed_permanent` | SET | Terminal-failure membership (retries exhausted) | Added when attempts exceed `max_retries`, permanent |
| `crawler:{ns}:domain:{domain}:queue` | ZSET, score=`priority*1e6+seq` | Per-domain ready queue | Entries added by `add_url`, reclaim, or retry-promotion; removed on claim. Redis auto-deletes the key when empty. |
| `crawler:{ns}:domain:{domain}:next_time` | STRING (float, not floored) | Per-domain rate-limit gate | Set on every successful claim from that domain |
| `crawler:{ns}:domain_heads` | ZSET, score = priority of that domain's current queue head | Cross-domain global-priority index — the Redis analogue of the local frontier's `heapq` | Resynced (`ZADD`) to the domain queue's true head every time that queue is mutated (add/claim/requeue); removed when the domain queue empties |
| `crawler:{ns}:domains:active` | SET | Domains currently represented in `domain_heads` | Mirrors `domain_heads` membership; used for `get_status_counts`/debugging, not the hot claim path |
| `crawler:{ns}:inflight` | ZSET, score=`lease_expires_at` | Claimed-but-not-completed URLs | Added on claim, removed on completion or reclaim |
| `crawler:{ns}:claim:{url}` | HASH `{token, attempt, domain, priority, claimed_at}` | The CAS record completion/renewal validate against | Created on claim, deleted on completion or reclaim; **not present** = URL isn't currently claimed |
| `crawler:{ns}:attempts:{url}` | STRING (int) | Durable attempt counter, survives across requeue cycles | INCR'd on each claim; deleted on any terminal transition |
| `crawler:{ns}:retry_scheduled` | ZSET, score=`not_before` epoch | Backoff holding area for retryable failures | Added by `mark_failed`/reclaim when retrying; removed + requeued into the domain queue once due |
| `crawler:{ns}:meta:{url}` | HASH `{source_query, domain, priority, first_seen}` | Descriptive metadata needed to requeue from `retry_scheduled`/reclaim and to answer `get_source_query` | Created at `add_url`; **kept alive for the URL's entire active lifetime**; only deleted/TTL'd after a terminal transition (§9) |

Notes:
- `urls:queued` from Revision 1 is dropped — membership is now derivable (`known` minus everything in a terminal set), and dedup no longer depends on it.
- `attempts:{url}` is kept separate from `claim:{url}` deliberately: the claim hash is ephemeral (exists only while in-flight), but the attempt count must survive the `INFLIGHT → RETRY_SCHEDULED → QUEUED → INFLIGHT` cycle, so it can't live inside a structure that gets deleted on every claim completion.

### Atomic operations (all as single Lua scripts, called via `register_script` as today)

1. **`add_url`** (1 round trip): `SADD known` (abort if already member) → `INCR seq` → `ZADD domain:{d}:queue` → resync `domain_heads`/`domains:active` → `HSET meta:{url}`.
2. **`claim_next(now, K, lease_ttl)`** (1 round trip): `ZRANGE domain_heads 0 K-1 WITHSCORES` for up to `K` candidate domains (bounded — see §6 for why); for each, in priority order: check `next_time`, skip if rate-gated; if eligible, pop the domain queue's head, update `next_time`, resync `domain_heads`/`domains:active` for that domain, `INCR attempts:{url}`, `HSET claim:{url}`, `ZADD inflight`. Returns the claimed URL + attempt, or `nil` if all `K` candidates were rate-gated/empty.
3. **`complete_claim(url, token, outcome, ...)`** (1 round trip; shared by `mark_visited`/`mark_failed`/`mark_skipped`): `HGET claim:{url} token`; mismatch → return `"stale"` no-op; match → `ZREM inflight`, `DEL claim:{url}`, then branch: visited → `SADD visited`, `DEL attempts`; skipped → `SADD skipped`, `DEL attempts`; failed → check `attempts:{url}` vs `max_retries`, either `ZADD retry_scheduled` (with computed backoff) or `SADD failed_permanent` + `DEL attempts`.
4. **`renew_claim(url, token, lease_ttl)`** (1 round trip): `HGET claim:{url} token`; mismatch → return `nil`; match → `ZADD inflight now+lease_ttl`, return new expiry.
5. **`reclaim_and_promote(now, batch_size)`** (1 round trip, run by the background task): (a) `ZRANGEBYSCORE inflight -inf now LIMIT 0 batch_size` — for each, read `claim:{url}` for domain/attempt, `DEL claim:{url}`, `ZREM inflight`, then same retry-vs-terminal branch as above, reading domain/priority from `meta:{url}` when requeuing; (b) `ZRANGEBYSCORE retry_scheduled -inf now LIMIT 0 batch_size` — for each, `ZREM retry_scheduled`, read `meta:{url}`, `ZADD domain:{d}:queue`, resync `domain_heads`.

**Round-trip accounting** (the thing Revision 1 got wrong): `add_url` = 1, `claim_next` = 1, every completion = 1, `renew_claim` = 1, one recovery tick = 1 (both phases folded into one script). Nothing is O(domains) network round trips — `claim_next`'s `K`-bounded loop over `domain_heads` is O(K) **server-side CPU inside one round trip**, not K round trips. `K` (default e.g. 50) caps worst-case Lua execution time when many domains are simultaneously rate-limited; if all `K` candidates are gated, `claim_next` returns `nil` and the caller backs off, same as today's idle-poll behavior in the scheduler loop.

---

## 6. Global priority + rate limiting algorithm

Direct port of `core/url_frontier.py`'s `heapq`/`_schedule_domain`/`blocked_domains` pattern onto Redis structures:

- **Local:** `priority_queue` (heap of `(priority, seq, domain)`) picks the globally-cheapest domain; if it's rate-gated, it's set aside in `blocked_domains` and the next-cheapest domain is tried, until one is eligible or the heap is exhausted; all blocked domains are re-pushed at the end so they're reconsidered next call.
- **Redis:** `domain_heads` (zset scored by each domain's current head priority) plays the role of `priority_queue`. `claim_next`'s Lua loop walks it in score order exactly like the heap pops, skipping (not removing) rate-gated domains — since nothing is popped from `domain_heads` for a skipped domain, there's no re-push step needed, it's just naturally still there next call. The only Redis-specific addition is the `K` bound, needed because a Lua script must have bounded worst-case runtime (the in-process heap has no such constraint since Python isn't running inside a single-threaded atomic transaction the way Redis is).
- **Rate limiting:** unchanged in spirit from Revision 1 — `domain:{d}:next_time` gates claims per domain — but now stored as a float string, not `math.floor`'d, fixing the sub-second `rate_limit` precision loss (0.3s default was collapsing to ≥1s).
- **Atomicity under concurrent workers:** the entire pick-domain → check-rate-gate → pop-URL → set-next_time → issue-claim sequence is one Lua script, so two workers calling `claim_next` concurrently are serialized by Redis's single-threaded script execution — no two workers can ever receive the same URL, which is the property `redis_frontier_test.py::test_get_next_url_no_duplicates` already checks and must keep passing.

---

## 7. Recovery algorithm (asyncio background task, in-process)

Per Decision 2: a dedicated `asyncio.Task` in `core/crawler_manager.py`, not reliance on `get_next_url()` being called:

```python
async def _recovery_loop(self):
    while not self._stop_event.is_set():
        try:
            self.frontier.reclaim_and_promote(batch_size=self.config.reclaim_batch_size)
        except Exception as e:
            logger.error(f"Recovery sweep failed: {e}")
        await asyncio.sleep(self.config.recovery_interval)
```

- Started alongside the scheduler/worker tasks in `crawler_manager.run()` (or per-backend `run()`, wherever `scheduler_task`/`workers` are currently gathered — see `async_crawler.py:224` and equivalents), included in the same `asyncio.gather(...)` / stopped via the same `_stop_event`.
- Runs independently of whether `get_next_url()` is being called, so it keeps sweeping even when the queue is fully drained except for one crashed worker's orphaned claim — directly addressing the scenario in Decision 2 (queue exhausted, scheduler stops polling, expired claim would otherwise never be reclaimed).
- For the **local frontier**, this loop is still wired up for interface consistency and to promote `retry_scheduled` backoff entries, but there is no "crash" scenario to guard against in-process (if the process dies, all in-memory frontier state dies with it — no different from today, and no worse than before this redesign). Its `reclaim_and_promote` there only needs to promote due backoff entries; there's no lease-expiry branch to run since a claim's completion is synchronous within the same process that issued it. Implementation-wise this can be as simple as a small `(ready_time, priority, seq, url)` min-heap checked lazily, rather than a literal periodic task — see §10.
- Interval is configurable (`recovery_interval`, default 30s) — decoupled from `lease_ttl` so operators can tune sweep frequency independent of how long a lease is valid.

---

## 8. Claim renewal — decision: **implement it** (Option 2)

**Tradeoff:**
- *Fixed long lease only*: simplest, but forces one number to cover the worst case across all 6 backends. Selenium/Playwright already configure 20–30s timeouts × 2–3 attempts (~60–90s worst case *today*), and this repo's target sites are adversarial (anti-piracy crawling) — slow TLS/Tor circuit negotiation, JS-heavy pages, deliberate rate-throttling by the target are all plausible and can exceed configured timeouts before they fire (e.g. TCP-level stalls). A lease long enough to never falsely expire a legitimate slow fetch (minutes) directly undermines the stated goal of crash recovery: a genuinely dead worker's claim would sit unreclaimed for that same multi-minute window.
- *Renewal/heartbeat*: decouples "how long is a single fetch allowed to legitimately run" from "how fast do we detect a dead worker." `lease_ttl` can stay short (default 90s) because a live worker keeps proving liveness via `renew_claim`; a crash is detected within one `lease_ttl` regardless of how long fetches are permitted to run.

**Recommendation: renewal.** All 6 backends already `await` their `fetch()` call from an `async def worker()` — even Selenium is offloaded via `asyncio.to_thread` (`selenium_crawler.py:123`), so the event loop stays free to run a concurrent heartbeat. Implement once, shared, not duplicated per backend:

```python
# core/claim_heartbeat.py
async def run_with_heartbeat(frontier, claim, coro, heartbeat_interval):
    task = asyncio.ensure_future(coro)
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=heartbeat_interval)
        if task in done:
            break
        renewed = frontier.renew_claim(claim)
        if renewed is None:
            task.cancel()
            raise ClaimLostError(claim.url)
        claim = renewed
    return task.result(), claim
```

Each of the 6 `worker()` methods wraps its existing `self.fetch(...)` call with this instead of calling it directly — a one-line change per file (§11), no logic duplicated. `renew_claim` validates `claim.token` exactly like the completion methods (§1, §3) — a renewal from a worker whose claim was already reclaimed fails closed (`None`), and `ClaimLostError` short-circuits that worker out of processing a URL another worker now owns.

---

## 9. Metadata lifecycle

Per the explicit correction: **no blind fixed TTL on active metadata.**

- `crawler:{ns}:meta:{url}` is written once at `add_url` (`source_query`, `domain`, `priority`, `first_seen`) and is **required, load-bearing state** for as long as the URL is anywhere in `{QUEUED, INFLIGHT, RETRY_SCHEDULED}` — `reclaim_and_promote` and retry-promotion both read it to know where to requeue the URL. Deleting or expiring it early would silently break retries/reclaim (a promoted URL with no known domain/priority).
- It is only eligible for cleanup once the URL reaches a **terminal** state: `mark_visited` and `mark_skipped` handlers, and the `failed_permanent` branch of `complete_claim`/`reclaim_and_promote`, are the only places that may `DEL` or set a TTL on `meta:{url}`.
- Whether terminal metadata should be deleted immediately or TTL'd for a retention window is a product/retention-policy question, not a correctness one — proposal: apply a configurable TTL (`terminal_meta_ttl_seconds`, default e.g. 3 days, `0`/`None` = delete immediately) only at the terminal transition, so it's explicit and reviewable rather than baked into the frontier's core logic. Not deciding the exact retention number here; flagging it as a follow-up subject to whatever the project's actual retention requirement turns out to be.
- `crawler:{ns}:attempts:{url}` follows the same rule — alive exactly as long as the URL is active, deleted at the same terminal transition.

---

## 10. SQLite / local-frontier compatibility

**Preserve exactly** (this is the behavioral reference; nothing below changes):
- Dedup via `visited` ∪ `_queued` (now folded into the "known" concept, §5) plus the `url_database.is_visited` cross-check.
- Per-domain rate limiting via `domain_next_time`.
- Global cross-domain priority via the `heapq` of `(priority, seq, domain)`.
- Blacklist-becomes-active-while-queued eviction behavior (`tests/frontier_test.py::test_frontier_drops_queued_urls_when_domain_becomes_blacklisted`).
- Already-visited-in-database skip (`tests/frontier_test.py::test_frontier_skips_urls_already_visited_in_database`).

**Intentionally changed** (do not port Redis's internal data structures here — same behavior, simplest possible in-process implementation):
- `get_next_url()` now returns `FrontierClaim | None` instead of `str | None`. Internally trivial: `_active_claims: dict[str, str]` (url → current token) and `_attempts: dict[str, int]` replace the need for any Redis-style lease bookkeeping — a claim's completion is always synchronous within the same process, so there's no crash-recovery scenario to build for locally, only token validation for interface parity and to catch double-completion bugs.
- `mark_failed` becomes real (today it doesn't exist on `URLFrontier` at all): attempt tracked in `_attempts`, retry-vs-terminal decided against the same `max_retries`/`base_backoff` config as Redis. Backoff-pending entries live in a small `(ready_time, priority, seq, url)` min-heap, drained lazily at the top of `get_next_url()` (any entry whose `ready_time <= now` is moved into the normal `domain_queues`/`priority_queue` before the normal claim logic runs) — no separate task needed locally since `get_next_url` is already polled continuously by the scheduler; the asyncio recovery task (§7) still runs for symmetry but has nothing lease-related to do here.
- `get_status_counts()` is added (didn't exist before) with the same six buckets as Redis.

**Optional, deferred, not required for v1:** an additive `attempts INTEGER DEFAULT 0` column on `storage/url_database.py`'s `urls` table would let attempt counts survive a process restart (today, restarting the process resets in-memory `_attempts` to zero, meaning a URL that had failed twice before a restart gets `max_retries` fresh tries again). This is a minor durability gap, not a correctness one (SQLite is a secondary audit/persistence layer here, not the frontier's source of truth), and can be revisited if restart-durability of attempt counts turns out to matter.

---

## 11. Required crawler call-site changes

All 6 files (`crawler/{async,http,tor,playwright,selenium,scrapling}_crawler.py`) plus `hybrid_crawler.py` and `core/scheduler.py:34` need the same mechanical edit, since none share a base class:

```diff
- url = self.frontier.get_next_url()
+ claim = self.frontier.get_next_url()
+ url = claim.url if claim else None
  ...
  if URLUtils.is_blacklisted(url):
-     self.frontier.mark_visited(url)
+     self.frontier.mark_skipped(claim)
      ...
      continue
  ...
- html, failure_reason = await self.fetch(session, url, ...)
+ (html, failure_reason), claim = await run_with_heartbeat(
+     self.frontier, claim, self.fetch(session, url, ...), heartbeat_interval
+ )
  ...
- self.frontier.mark_visited(url)
+ if failure_reason:
+     self.frontier.mark_failed(claim, failure_reason)
+ else:
+     self.frontier.mark_visited(claim)
```

And the outer catch-all (`except Exception as exc: logger.error(...)`) in every file gains a `self.frontier.mark_failed(claim, str(exc))` (guarded by `if claim is not None`) instead of silently dropping the URL — closes the "parser/media-DB exception leaks the claim until lease expiry" gap noted in §0, using explicit failure instead of relying on the recovery sweep as the only backstop.

`hybrid_crawler.py`'s engine-escalation logic (lines ~225–267, where it retries with a different engine on `failure_reason`) needs care: an escalation-to-better-engine is not the same as a frontier-level failure — it should keep the same claim/token if it's retried in-process within the same `worker()` call (no new `get_next_url()`), and only call `mark_failed`/`mark_visited` once a final outcome (across all attempted engines) is known. This is a slightly larger review than the mechanical edit above and worth a closer pass when this file is touched.

`core/scheduler.py:34` (the only other caller of `get_next_url()`) needs the same `claim`-instead-of-`url` update — currently a stub, worth checking its actual contents before editing.

---

## 12. Tests required before implementation

**Local frontier (`tests/frontier_test.py`, extend):**
- Existing 4 tests must keep passing unmodified in behavior (only the `get_next_url()` return type changes, so assertions comparing to raw URL strings need `claim.url` accessors).
- New: `mark_failed` below `max_retries` requeues with backoff and increasing `attempt`; at `max_retries` goes to `failed_permanent`.
- New: stale claim (token from a prior claim on the same URL, after it was already completed) rejected by `mark_visited`/`mark_failed`/`mark_skipped` — no state change, no exception.
- New: `get_status_counts` buckets sum to total known URLs, every URL in exactly one bucket.

**Redis frontier (`tests/redis_frontier_test.py`, extend — needs live Redis, already skip-if-unavailable):**
- Existing dedup/concurrent-add/no-duplicate-claim/rate-limit/namespace-isolation tests must keep passing.
- New: two workers, one claims a URL, its lease is force-expired (test hook to backdate `inflight` score), a second `claim_next` call reclaims it; first worker's subsequent `mark_visited(stale_claim)` is rejected; second worker's `mark_visited(new_claim)` succeeds.
- New: `mark_failed` × N below `max_retries` → requeued each time with growing backoff; Nth+1 → `failed_permanent`, confirmed via `get_status_counts`.
- New: `renew_claim` extends `inflight` score for a live claim; returns `None` for an already-reclaimed one.
- New: priority ordering across ≥3 domains verified via `domain_heads` (a low-priority URL on a domain added later must still be claimed before a high-priority-number URL on a domain added earlier) — this is the test that would have caught Revision 1's SCAN-order bug.
- New: `reclaim_and_promote` round-trip count — assert it's O(1) Redis calls per invocation regardless of domain count (guards against regressing back to SCAN-style O(domains) behavior).
- New: crash-injection style test — claim a URL, never complete it, force-expire its lease, run one recovery sweep, assert it's claimable again (or `failed_permanent` if attempts exhausted).

**Config:** validation tests for the new `FrontierConfig` fields (sane defaults, rejects negative `lease_ttl`/`max_retries`, etc.) since these are new user-facing knobs.

---

## 13. Migration order

1. Introduce `FrontierClaim` + the `Frontier` protocol; update `core/url_frontier.py` to the new contract (claim tokens, real `mark_failed`, `get_status_counts`) with no lease machinery needed in-process. Lowest risk, fully testable without Redis, and it's the behavioral reference everything else is checked against.
2. Update `core/scheduler.py` and all 6 crawler backends to the claim-based call sites (§11), running against the local frontier only (existing default). Full existing test suite plus §12's new local tests green before moving on.
3. Implement the Redis v2 keyspace + Lua scripts (§5) in `core/redis_frontier.py`, unit-tested in isolation the same way `tests/redis_frontier_test.py` already does (skip-if-no-Redis).
4. Wire the asyncio recovery task (§7) and new config knobs into `core/crawler_manager.py`, gated to run for both frontier types (no-op-ish for local, load-bearing for Redis).
5. Add `renew_claim` + the shared `run_with_heartbeat` helper (§8), thread it through all 6 worker loops.
6. Run the full new Redis multi-worker test suite (§12) plus a manual crash-injection soak test (`kill -9` a worker process mid-claim, confirm reclaim within `lease_ttl + recovery_interval`).
7. Update `REDIS_MULTIWORKER_SUMMARY.md` / `docs/DISTRIBUTED_SETUP.md` to document the new config knobs (`lease_ttl`, `max_retries`, `base_backoff`, `max_backoff`, `recovery_interval`, `reclaim_batch_size`) and corrected performance/architecture description.

Still no code changes made. Waiting for approval to begin step 1.
