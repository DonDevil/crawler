# Anti-Piracy Crawler — Architecture Audit (Read-Only)

## 1. Actual architecture & execution flow

Single OS process, single `asyncio` event loop. "Workers" are `asyncio.Task`s, not threads/processes (except Selenium's blocking calls, pushed off-loop via `asyncio.to_thread`).

```
main.py (argparse)
  → CrawlerManager (core/crawler_manager.py)
      ├─ load_config()              config.yaml → Pydantic models (core/config.py)
      ├─ URLDatabase + DomainDatabase   SQLite, always created regardless of frontier backend
      │  (+ MediaEvidenceDatabase, separate file)
      ├─ frontier = URLFrontier (in-memory heap) | RedisURLFrontier   per config.crawler.frontier.type
      ├─ crawler = HybridCrawler | AsyncCrawler | HTTPCrawler | TorCrawler
      │            | PlaywrightCrawler | SeleniumCrawler | ScraplingCrawler
      ├─ prepare_frontier(): seed files  and/or  search-query discovery  and/or  resume-from-DB (mutually exclusive)
      └─ crawler.run(): internal scheduler() task + N worker() tasks pull from frontier,
                         fetch, extract links/media, requeue links, persist, close
```

`core/crawler_router.py` is a separate, narrower thing: it's used *only inside* `HybridCrawler` to pick/escalate a per-URL fetch strategy (tor for onion, async→scrapling→playwright→selenium escalation on JS/captcha signals). It is not part of top-level orchestration and has no overlap with `crawler_manager.py`.

## 2. Key modules & responsibilities

| Layer | Real/working | Dead / orphaned scaffolding |
|---|---|---|
| Orchestration | `crawler_manager.py`, `config.py` | — |
| Frontier | `core/url_frontier.py` (in-memory heap + SQLite mirror), `core/redis_frontier.py` | `core/worker_pool.py`, `core/scheduler.py`, `core/rate_limiter.py` — all fully-written, zero callers anywhere |
| Fetch backends | `crawler/{async,http,tor,playwright,selenium,scrapling,hybrid}_crawler.py` — all 6 are real, complete, independently runnable | `utils/retry_handler.py` (tenacity-based, unused; every backend hand-rolls its own retry loop instead), `tor/tor_manager.py`, `tor/onion_router.py` (assume an already-running external Tor daemon; nothing launches Tor) |
| Discovery | `discovery/search_engine_discovery.py`, `discovery/piracy_site_seeds.py`, `search_engines/*` (6 real scrapers + base ABC) | `discovery/domain_expander.py`, `discovery/darkweb_discovery.py`, `discovery/torrent_site_discovery.py`, `search_engines/custom_query_generator.py` (identity no-op), `intelligence/*` (all three files) |
| Extraction | `parsers/html_link_extractor.py`, `javascript_link_extractor.py`, `media_link_detector.py`, `streaming_manifest_parser.py`, `utils/url_utils.py` | `parsers/page_metadata_parser.py`, `storage/result_exporter.py` |
| Storage | `storage/url_database.py`, `storage/domain_database.py` (schema real but never queried for logic), `storage/media_evidence_database.py`, `storage/async_database_writer.py` | `storage/crawl_state_db.py` — its `state` table doesn't even exist in the live `.db` file; confirmed zero callers |

## 3. SQL frontier design — why it works

Important correction to the framing: **the "SQL frontier" is not actually SQL-driven.** `core/url_frontier.py` is a pure in-process Python priority structure — a global min-heap of `(priority, sequence, domain)` plus a `dict[domain → deque[(priority, seq, url)]]`. SQLite (`storage/url_database.py`) is only a **dedup/crash-recovery mirror**: `add_url()` writes `status="queued"` for resume visibility, `is_visited()` is consulted to skip already-completed URLs across restarts. It is never queried to decide what to crawl next.

- **Schema**: `urls(url PK, first_seen, last_seen, status)`, `domains(domain PK, first_seen, last_seen, score)` — both default to the same file, `storage/crawl_state.db`, via independent `sqlite3.connect()` calls. No indexes beyond the PK.
- **State machine**: `queued → pending → visited|failed|skipped`. The upsert uses a status *ratchet* (`ON CONFLICT ... CASE WHEN status='visited' THEN status ELSE excluded.status END`) so a re-crawl can never demote a `visited` record.
- **Why no locking is needed**: `crawler_manager.py` logs it outright — `"Using SQLite frontier (single-worker mode)"`. `get_next_url()` is a synchronous, non-`await`ing method; under asyncio's cooperative scheduling nothing can interleave inside it. Correctness comes from there being **no cross-process contention to coordinate**, not from any locking discipline.
- **Reliability factors**: WAL mode + `busy_timeout=5000` on every connection, single logical writer per file, `BatchedDatabaseWriter` guarded by a `threading.Lock`.
- **Dead weight discovered**: `BatchedDatabaseWriter` is misleadingly named — it calls `_flush()` (commits) after *every single* `execute()`; the `batch_size` constructor param is stored but never read. `DomainDatabase.score` is computed and stored but never consulted anywhere for prioritization or rate-limiting — it's inert.

## 4. Redis frontier — specific problems

**Data model**: per-domain ZSET `{ns}:urls:domain:{domain}:queue` (score = `priority*1e6+sequence`, semantically matches SQL's ordering), global SETs `urls:queued`/`urls:visited`, per-URL metadata HASH, per-domain rate-limit key. Add/dequeue/mark-visited are Lua scripts — the intent was atomicity, and add/mark-visited genuinely achieve it.

**The core, now-being-fixed bug**: at HEAD (commit `97c64b2`), `get_next_url`'s Lua script `zrange`'d the domain queue head and set the rate-limit timer, but **never removed the URL from the ZSET or the `queued` SET**. Two workers (or the same worker on a later poll) could dequeue the identical URL repeatedly — the opposite of the atomicity the class docstring claims. The currently uncommitted local diff (`git diff -- core/redis_frontier.py tests/redis_frontier_test.py`) adds the missing `zrem`/`srem` calls — a correct, narrow, in-progress fix, not a rewrite. Fixing this trades one problem for another: post-fix, there's still **no in-flight/claimed state and no lease/visibility-timeout**, so a worker that crashes after claiming a URL loses it permanently — never requeued, never retried.

**Priority model diverges from SQL, silently**: priority only orders *within* one domain's ZSET. Which domain gets serviced next is decided by Redis `SCAN` keyspace-iteration order over `urls:domain:*:queue` — effectively arbitrary hash-bucket order. SQL's global heap does real cross-domain priority scheduling; Redis does not. A high-priority URL on domain B can sit behind low-priority work on domain A indefinitely.

**Why it's slower than SQL (ranked by confidence)**:
1. **Blocking sync `redis` client (not `redis.asyncio`) called directly inside `async def scheduler()`.** Every Redis round trip stalls the *entire event loop*, freezing all concurrently-running fetch workers — this alone can dominate wall-clock time as concurrency scales up.
2. `get_next_url()` does a full `SCAN` cursor loop over every domain-queue key, then one Lua `EVALSHA` round-trip *per matching domain* just to check its rate-limit gate — O(domains) network round-trips per single URL claimed, versus SQL's O(log n) pure in-memory heap pop.
3. `add_url()` costs an `INCR` + a Lua eval **plus** a separate synchronous SQLite write to the same `url_database` mirror that SQL-mode uses — Redis mode pays Redis I/O *and* the identical disk-committing SQLite write, strictly more I/O per operation, not less.
4. No pipelining anywhere.

**Observability is structurally broken, not just "hard to inspect"**: `get_status_counts()` only ever returns `queued`/`visited` — `mark_failed`/`mark_skipped` both write into the `urls:visited` SET ("treat failed same as visited for dedup purposes"), destroying the distinction at write time. `tests/report.py`'s `--redis` mode reads `{ns}:urls:failed`/`{ns}:urls:skipped` as if they were separate keys — **they are never written**, so it silently reports 0 failures/skips regardless of what actually happened during a crawl. This is a concrete, demonstrable bug, not a vague complaint.

**Test coverage gaps**: `tests/redis_frontier_test.py` genuinely exercises concurrent add/dequeue-dedup/rate-limiting with real `ThreadPoolExecutor` + real Redis (skips if unavailable, not mocked) — solid as far as it goes. It does not test crash-mid-claim recovery, cross-domain priority ordering, or the failed/skipped bucket bug.

## 5. Current URL state/priority model

Status vocabulary: `queued → pending → visited | failed | skipped` (SQL); Redis mirrors a reduced `queued`/`visited` (failed+skipped folded in). Two independent scorers feed the same `priority: int` (lower = sooner) into whichever frontier is active:
- `URLUtils.get_link_priority()` (in-page discovered links): same-domain=8, onion=9, piracy-hint=11, other=20, ad/blacklist=50, plus a query-token boost.
- `score_discovered_url()` (search-engine results): `engine_priorities[engine] + min(rank, 10)`, reduced by `onion_priority_boost` for `.onion` results.

`intelligence/piracy_domain_classifier.py` plays **no role** in this scoring path — it's used only by `crawler_router.py` to pick a fetch *engine strategy*, not frontier priority. `intelligence/domain_reputation.py` and `intelligence/duplicate_url_filter.py` are dead code, never imported outside their own files/tests.

## 6. Worker and concurrency behavior

No shared worker pool actually drives the app — `core/worker_pool.py` and `core/scheduler.py` are fully-written but orphaned; **each of the 6 fetch backends independently reimplements the identical pattern**: an `asyncio.Queue`, `N = concurrency` worker tasks via `asyncio.create_task`, one `scheduler()` task pumping the frontier into the queue, with idle-loop-count-based shutdown (~10 empty polls ≈5s + `frontier.has_pending()==False`). This is ~100 lines of copy-pasted boilerplate across 6 files — `worker_pool.py`/`scheduler.py` look like an abandoned attempt to extract exactly this shared logic.

`core/rate_limiter.py` (a clean `aiolimiter`-based token bucket) is also orphaned — actual rate limiting is per-domain and lives inline in `URLFrontier.get_next_url()`/`RedisURLFrontier`. Retry logic is hand-rolled per backend (fixed `sleep(1)` between attempts) rather than using the also-orphaned `utils/retry_handler.py` (tenacity-based, real, unused).

Concurrency caps are backend-specific magic numbers: Playwright caps at 8, Selenium at 4, hybrid uses `asyncio.Semaphore`s hardcoded at `min(10/5/3/2, concurrency)` per engine type. No SIGINT/SIGTERM handler is registered anywhere — clean shutdown on Ctrl+C is not guaranteed.

Surface-web vs. dark-web is decided per-URL by `URLUtils.is_onion_url()`; `CrawlerRouter` forces onion URLs straight to the Tor backend with no escalation chain, using an already-running local Tor daemon detected via `proxy_config.get_default_tor_proxy()` (`127.0.0.1:9050`/`9150` or env vars) — nothing in the codebase launches Tor itself.

## 7. Search-engine/discovery architecture

Solid and real. `search_engines/base.py` is an ABC (`search(query, max_results) -> list[str]`) with shared `httpx`+BeautifulSoup fetch/parse helpers and a typed exception hierarchy. All 6 concrete engines (DuckDuckGo, Bing, Brave, Yandex, Ahmia, Torch) are genuine HTML-scraping integrations — **no official search APIs, no API keys** — each with engine-specific redirect-unwrapping logic; Yandex self-detects its own captcha wall and raises rather than silently failing; Torch requires a live Tor SOCKS proxy.

`discovery/search_engine_discovery.py` is the real orchestrator behind `--query`/`--query-only`/`--surface-web`/`--dark-web`: sequential per-engine calls with independent try/except (one engine failing doesn't kill the batch), a cooldown mechanism for blocked engines, and `score_discovered_url()` feeding priority into `frontier.add_url()`.

Dead code discovered here: `custom_query_generator.py` is an identity no-op (no actual query expansion happens anywhere); `discovery/domain_expander.py`, `darkweb_discovery.py`, `torrent_site_discovery.py` are all unreferenced outside their own files — seed loading in the real pipeline calls `piracy_site_seeds.load_seeds()` directly, bypassing all three wrapper modules; `intelligence/duplicate_url_filter.py` is unused — actual dedup is duplicated inline as ad-hoc `set()` logic in two places inside `search_engine_discovery.py`.

## 8. Extraction/media pipeline

Real end-to-end. `HTMLLinkExtractor` (BeautifulSoup+lxml) plus a regex-based JS-URL extractor (not a real JS parser — no AST/headless hook) feed `URLUtils.clean_url()`/`should_queue_link()` for normalization, tracking-param stripping, and crawler-trap detection. `MediaLinkDetector` and `StreamingManifestParser` (genuine HLS/DASH parsing) route matches into a **separate** `MediaEvidenceDatabase` (`storage/media_evidence.db`, distinct file from the crawl-state DB) with a real pull-queue contract — `claim_next_sample_job`/`complete_sample_job`/`mark_asset_matched` — that `tests/fingerprinter_queue_test.py` exercises. **This is the actual, currently-working interface point toward the fingerprinter** and is worth preserving verbatim when that integration is designed later. `page_metadata_parser.py` and `storage/result_exporter.py` are unwired dead code.

## 9. Current CLI behavior (ground truth, not docs)

Several flags in the original brief don't exist under those names — worth correcting before further planning:

| Brief said | Reality |
|---|---|
| `--redis` / `--sql` | **Don't exist.** Frontier backend is config-only: `config.yaml: crawler.frontier.type`. **Current `config.yaml` default is `"redis"`** — i.e. the app currently runs on the known-broken backend by default. |
| `--search-engines` | **Doesn't exist.** Engine selection is `config.yaml: search.enabled_engines`, filtered by `--surface-web`/`--dark-web`. |
| `--max-crawl` | Actual flag is `--max-pages`; `--indefinite-run` sets it to `None`. |
| `--seed-files` | Actual flag is `--seed-file` (singular, `action="append"`, repeatable). |
| resume/continue | `--unfinished`, mutually exclusive with `--query-only`. Loads `queued`/`pending` rows from SQLite only — **there is no Redis-side resume path**. |

Also present, outside the original brief: `--crawler-engine {auto,async,http,tor,playwright,selenium,scrapling}`, `--debug`, `--clear-db`, `--ignore-blacklist`, and a fingerprinter-adjacent side-channel mode (`--claim-sample-job`, `--worker-name`, `--mark-match`, `--match-title`, `--match-confidence`) that bypasses `CrawlerManager` entirely and talks straight to `MediaEvidenceDatabase` — this is the CLI surface of the interface noted in §8.

## 10. Technical debt and architectural problems

- **Operationally live risk**: config defaults to the broken Redis frontier right now.
- **Six fully-written, zero-caller modules**: `core/worker_pool.py`, `core/scheduler.py`, `core/rate_limiter.py`, `utils/retry_handler.py`, `tor/tor_manager.py`, `tor/onion_router.py` — each duplicated inline instead, or never adopted.
- **Five more dead/orphaned files** in discovery/intelligence: `domain_expander.py`, `darkweb_discovery.py`, `torrent_site_discovery.py`, `custom_query_generator.py`, `duplicate_url_filter.py`, plus `intelligence/domain_reputation.py` and `storage/crawl_state_db.py` and `storage/result_exporter.py` and `parsers/page_metadata_parser.py`.
- **No shared crawler-backend interface/base class** — 6 backends duplicate ~100 lines of worker/scheduler/retry boilerplate each, direct violation of the "single-responsibility, no cleverness" goal and the reason `worker_pool.py`/`scheduler.py` exist as an abandoned extraction attempt.
- **Global mutable state**: `URLUtils._blacklist_domains` class-level cache mutated with file I/O on the hot classification path, no lock (currently safe only because asyncio is cooperative and the calls have no internal `await` — a real risk if that ever changes); `URLUtils.set_blacklist_enabled()` toggles class-level state from `crawler_manager.py`.
- **Misleading names**: `BatchedDatabaseWriter` doesn't batch; `RedisURLFrontier`'s Lua-script docstring claims atomicity it (at HEAD) doesn't provide.
- **Bare/broad excepts** in several places: `url_utils.py` (multiple `except Exception: return None/False`), `bing_search.py:31-32` (swallows base64 decode errors), `domain_expander.py:18`, `selenium_crawler.py` (`driver.quit()` wrapped in `except Exception: pass`).
- **No graceful shutdown** (SIGINT/SIGTERM) anywhere in the run loop.
- **Interface boundary violation**: business logic (frontier, extraction) isn't behind a `Protocol` — `RedisURLFrontier`/`URLFrontier` share a method surface by convention, not by declared contract, and `mark_failed`/`mark_skipped`/`get_status_counts` exist only on the Redis side with no SQL equivalent.
- Large under-typed constructors (`crawler_manager.py.__init__` ~120 lines mixing construction/engine-selection/config-resolution; several backend `__init__`s take untyped `frontier`/`parser`/`media_database` params).

## 11. Recommended target architecture (direction only)

- Formalize a `FrontierProtocol` (`add_url`, `get_next_url`, `mark_visited`, `mark_failed`, `mark_skipped`, `get_status_counts`, `pending_count`, `close`) that both backends implement identically — no Redis-only or SQL-only extras.
- Redis frontier rebuild, in order of leverage: (a) real atomic claim removing from ZSET+SET in the same Lua call (the in-flight uncommitted fix, generalized); (b) an in-flight/claimed ZSET with a lease timestamp so crashed-worker claims are reclaimed after a timeout; (c) `redis.asyncio` (or `asyncio.to_thread`-wrapped sync client) so it stops blocking the event loop; (d) single global-priority claim primitive instead of SCAN-over-domains, to restore true cross-domain priority ordering; (e) separate `failed`/`skipped` sets so observability isn't destroyed at write time; (f) drop or make genuinely async the redundant SQLite mirror write on the Redis hot path.
- Extract one shared `CrawlerWorkerLoop`/base class that the 6 fetch backends compose instead of copy-pasting scheduler/worker/retry logic — this is what `worker_pool.py`/`scheduler.py`/`retry_handler.py` were reaching for; either finish that extraction or delete them, not both.
- Move the blacklist cache out of `URLUtils` classmethods into an injected, explicitly-owned component — no global class-level mutable state.
- Decide DomainDatabase's fate: either wire `score` into rate-limiting/priority for real, or delete it — currently it's inert infrastructure.

## 12. Safe incremental refactoring plan (not executed)

1. **Characterization tests first**: assert `get_status_counts()` reports non-zero `failed`/`skipped` after a mark (currently fails); a crash-simulation test that kills a worker mid-claim and asserts requeue (currently fails). These make today's real bugs concrete and regression-proof before anything changes.
2. **Finish and land the in-flight Redis atomic-claim fix** (`zrem`/`srem` in `get_next_url`) — already drafted uncommitted; smallest possible change with the highest correctness payoff.
3. **Fix Redis observability**: separate `failed`/`skipped` sets, correct `tests/report.py` reads — directly answers the "hard to inspect" complaint, isolated to `redis_frontier.py`.
4. **Add the lease/visibility-timeout mechanism** behind the existing method surface — no caller changes needed elsewhere.
5. **Replace SCAN-over-domains with a single global-priority Lua claim** — the biggest performance lever, isolated to `redis_frontier.py`.
6. **Switch to `redis.asyncio`** (or thread-offload the sync client) — stops event-loop stalls; isolated, low-risk.
7. **Only after 2–6 are done and re-tested**: decide per dead module (delete vs. actually wire in) — cheap, no behavioral risk, deliberately last so cleanup doesn't get entangled with the correctness work.
8. **Not part of this plan but flagged**: switch `config.yaml` frontier default back to `"sql"` until step 6 lands, since the app currently defaults to running on the backend known to lose work silently.

Nothing has been modified. Ready for direction on which numbered item to start with.
