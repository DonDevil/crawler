# Developer Guide

This explains how to work on the crawler codebase. For what the system
does and why, read
[`docs/architecture/system-architecture.md`](architecture/system-architecture.md)
first — this document assumes you've read it. For setup, see
[`docs/installation.md`](installation.md).

## Repository structure

```
main.py                    CLI entry point
config.yaml                runtime configuration

core/                      orchestration, frontier, config
crawler/                   7 independent crawler-engine implementations
discovery/                 seed loading + search-engine discovery orchestration
search_engines/            6 search-engine scraping adapters + base class
parsers/                   link/media/manifest extraction from fetched HTML
storage/                   SQLite + Redis backends (URL DB, domain DB, media evidence)
intelligence/               PiracyDomainClassifier (fetch-routing only)
tor/                        Tor SOCKS proxy config (assumes an already-running daemon)
utils/                      URLUtils (dedup/blacklist/priority), logging, headers
seeds/                      seed URL lists (see below)
datasets/                   domain_blacklist.txt (gitignored, mutable runtime state)

tests/                      pytest suite
tests/benchmarks/           manual (non-pytest) benchmark CLI scripts
benchmark/results/           committed historical benchmark JSON output

docs/                       this documentation tree
```

## Package responsibilities

- **`core/`** — `crawler_manager.py` (`CrawlerManager`, the orchestrator;
  `build_media_evidence_store`) · `config.py` (pydantic config model,
  `load_config`) · `frontier.py` (`Frontier` protocol, `FrontierClaim`,
  `FrontierUnavailable`) · `url_frontier.py` (`URLFrontier`, local
  backend) · `redis_frontier.py` (`RedisURLFrontier`, production backend)
  · `frontier_executor.py` (`AsyncFrontier`, the sync→async offload
  boundary) · `claim_heartbeat.py` (`run_with_heartbeat`, shared by the
  frontier and Media Evidence) · `crawler_router.py` (`CrawlerRouter`,
  used only by `HybridCrawler`).
- **`crawler/`** — one file per engine, no shared base class:
  `async_crawler.py`, `http_crawler.py`, `hybrid_crawler.py`,
  `playwright_crawler.py`, `scrapling_crawler.py`, `selenium_crawler.py`,
  `tor_crawler.py`.
- **`discovery/`** — `piracy_site_seeds.py` (`load_seeds`, real) and
  `search_engine_discovery.py` (the real discovery orchestrator:
  `discover_urls_from_query(_with_report)`,
  `discover_urls_from_queries(_with_report)`, `score_discovered_url`,
  `build_search_engines`, `get_engine_names_for_scope`).
- **`search_engines/`** — `base.py` (`BaseSearchEngine` ABC,
  `SearchEngineError`/`SearchEngineUnavailableError`/
  `SearchEngineBlockedError`/`SearchEngineParsingError`) plus one adapter
  per engine (`ahmia_search.py`, `bing_search.py`, `brave_search.py`,
  `duckduckgo_search.py`, `torch_search.py`, `yandex_search.py`).
- **`parsers/`** — `html_link_extractor.py` (`HTMLLinkExtractor`),
  `media_link_detector.py` (`MediaLinkDetector`,
  `extract_media_links`), `streaming_manifest_parser.py`
  (`StreamingManifestParser`), `javascript_link_extractor.py`
  (`JavaScriptLinkExtractor`).
- **`storage/`** — `url_database.py` (`URLDatabase`, real, load-bearing:
  SQL-mode dedup authority + Redis-mode startup-seeding defer target) ·
  `domain_database.py` (`DomainDatabase`, real, constructed every run,
  scoring currently dormant) · `async_database_writer.py`
  (`BatchedDatabaseWriter` — note: despite the name, it commits after
  every single `execute()` regardless of `batch_size`; this is documented
  as deliberate, for write-visibility freshness, not an oversight — see
  [`docs/architecture/history/sql-persistence-audit.md`](architecture/history/sql-persistence-audit.md))
  · `media_evidence_store.py` / `sqlite_media_evidence_store.py` /
  `redis_media_evidence_store.py` (see
  [system-architecture.md §16](architecture/system-architecture.md#16-media-evidence-architecture))
  · `media_evidence_database.py` (a 1-line backward-compat alias,
  `MediaEvidenceDatabase = SQLiteMediaEvidenceStore` — don't import from
  it in new code).
- **`intelligence/piracy_domain_classifier.py`** — `PiracyDomainClassifier`,
  consumed only by `core/crawler_router.py` for fetch-engine selection.
  Never affects frontier priority.
- **`tor/proxy_config.py`** — `get_default_tor_proxy` /
  `get_httpx_tor_proxies`; assumes an external Tor daemon is already
  running and reachable, does not launch one.
- **`utils/url_utils.py`** — `URLUtils`, the largest single class in the
  codebase: URL cleaning/normalization, blacklist checks
  (`is_blacklisted`, `add_to_blacklist`), media classification, priority
  scoring (`get_link_priority`), trap/ad/adult-content heuristics.

### Known-dead code

The following files exist in the tree but have **zero live callers** —
confirmed by repo-wide grep, not inferred. Do not build new features on
top of them without first checking whether they're still genuinely
unreferenced, and do not describe them as part of the running system in
documentation: `core/scheduler.py`, `core/worker_pool.py`,
`core/rate_limiter.py`, `utils/retry_handler.py`,
`storage/crawl_state_db.py`, `storage/result_exporter.py`,
`parsers/page_metadata_parser.py`, `tor/tor_manager.py`,
`tor/onion_router.py`, `discovery/domain_expander.py`,
`discovery/darkweb_discovery.py`, `discovery/torrent_site_discovery.py`,
`search_engines/custom_query_generator.py`,
`intelligence/domain_reputation.py`, `intelligence/duplicate_url_filter.py`.

`path/` (containing `path/to/hello.py` / `path/to/hello.js`) is stray
scratch content, not an application package.

## Crawler execution path

See [system-architecture.md §3](architecture/system-architecture.md#3-crawler-execution-flow)
for the full sequence diagram. In short: `main.py` → `CrawlerManager` →
frontier (seed/resume/discover) → selected crawler engine's worker loop
(claim → fetch → extract → mark outcome) → recovery loop (Redis only).

## How configuration works

`core/config.py` defines a pydantic model tree:
`Config { crawler: CrawlerConfig { storage: StorageConfig, frontier:
FrontierConfig, media_evidence: MediaEvidenceConfig }, search: SearchConfig }`.
`load_config(path="config.yaml")` parses `config.yaml`, falling back to
all-pydantic-defaults if the file is missing. Storage paths are resolved
to absolute paths relative to the config file's own directory.

`config.yaml`'s Redis-related, non-default values (`domain_scan_limit`,
lease/recovery timings, the whole `media_evidence` block) carry inline
comments pointing at the architecture doc that justifies them — treat the
config file as partially self-documenting, and keep those comments
accurate if you change the values.

CLI flags in `main.py` override the equivalent config value for that run
only (`--crawler-engine`, `--media-backend`, `--max-pages`,
`--indefinite-run`) — they never write back to `config.yaml`.

## How Redis is used

Two independent Redis connections/namespaces by default (same physical
instance, different logical namespace): the frontier
(`crawler.frontier.redis_namespace`, default `crawler`) and Media Evidence
(`crawler.media_evidence.redis_namespace`, default `evidence`). Every
mutating operation on either is exactly one Redis round trip via a
server-side Lua script — this is what gives claim/complete/renew their
atomicity across concurrent machines. Full keyspace tables:
[system-architecture.md §7](architecture/system-architecture.md#7-frontier-architecture)
and [§17](architecture/system-architecture.md#17-media-evidence-redis-keyspace-conceptual).

**Rule: production Redis code must not silently fall back to SQLite.**
`FrontierUnavailable` and `MediaEvidenceUnavailable` exist specifically so
a Redis outage is visible to the caller, not swallowed. The frontier's
*construction* step is the one place that does fall back (Redis
connection failure at startup → falls back to the local `URLFrontier`,
logged as a warning) — that is a deliberate, narrow exception for
developer convenience at process start, not a runtime degradation path.
Media Evidence's construction does **not** have this fallback — treat that
difference as intentional, not an inconsistency to "fix."

## How SQLite is used

SQLite (`URLFrontier`, `SQLiteMediaEvidenceStore`) is an independent
dev/testing backend, selected via `frontier.type: "sqlite"` /
`media_evidence.type: "sqlite"`. It is not synchronized with Redis in
either direction. The one exception — `URLDatabase` used for a bounded
startup-seeding durable-defer path even in Redis mode — is documented in
[system-architecture.md §15](architecture/system-architecture.md#15-redissqlite-boundary);
don't extend that path's scope without re-reading that section.

## How to run tests

```bash
pytest                          # full suite
pytest tests/frontier_test.py   # one file
pytest -k redis                 # by keyword
```

There is no `pytest.ini`/`pyproject.toml`/`setup.cfg` — no custom markers
are registered. Redis-dependent tests self-skip at runtime (they attempt a
real connection in setup and call `pytest.skip` on `redis.ConnectionError`),
not via marker selection — if you want to actually run them, a local Redis
must be reachable. Browser-crawler tests
(`tests/extra_crawlers_test.py`) are gated behind an environment variable,
not a marker or skip-by-default logic:

```bash
RUN_BROWSER_CRAWLER_TESTS=1 pytest tests/extra_crawlers_test.py
```

Redis-dependent tests use isolated databases/namespaces so they never
touch production data: `tests/redis_frontier_test.py` → db 1, namespace
`test_crawler`; the Redis-failure-semantics and seed-failure tests → db 2,
distinct namespaces; `tests/redis_sqlite_mirror_removal_test.py` → db 1,
a per-test-random namespace. Production itself uses db 0.

`tests/frontier_executor_test.py::TestRedisFrontierIsOffloaded::test_concurrent_redis_calls_use_a_bounded_shared_thread_pool`
is a known pre-existing flaky test, unrelated to any specific feature —
don't spend time chasing it in isolation.

`tests/report.py` is **not** part of the pytest suite — it's a standalone
CLI reporting tool. See [`docs/REPORT_TOOL.md`](REPORT_TOOL.md). Its
`--redis` mode currently reports incorrect `queued`/`failed` counts
against a real production Redis instance (queries keys that don't exist
in the current keyspace) — a known code bug, not yet fixed; see
"Implementation discrepancies" in that doc.

## How to run benchmarks

Benchmark scripts live in `tests/benchmarks/` and are deliberately **not**
pytest-collected (their filenames don't match `test_*.py`/`*_test.py`).
See [`docs/benchmarks.md`](benchmarks.md) for what each measures and why,
and [`tests/benchmarks/README.md`](../tests/benchmarks/README.md) for the
full CLI flag reference. Quick examples:

```bash
python tests/benchmarks/frontier_benchmark.py --frontier redis --urls 10000 --workers 8
python tests/benchmarks/distributed_benchmark.py --workers 8 --urls 20000 --duration 30
python tests/benchmarks/media_evidence_benchmark.py --assets 500 --claim-workers 4
```

All Redis-backed benchmark scripts default to db 2 with a `bench*`
namespace prefix — isolated from both production (db 0) and the pytest
Redis suite (db 1).

## How to run a local crawler

```bash
python main.py --crawler-engine http --query "example query" --max-pages 50
```

See [`docs/installation.md`](installation.md#running) for a full set of
verified command examples covering every CLI flag.

## How to generate a crawl run report

`tests/report.py` reads current state from whichever backend the crawler
used and prints a human-readable summary (URL counts, throughput, resource
usage, Redis stats). It always reads state through the same production
classes the crawler uses (`RedisURLFrontier.get_status_counts()`,
`URLDatabase`), never a hand-maintained copy of the keyspace, so it can't
silently drift from the current frontier implementation the way the old
version did. A metric this tool cannot derive is printed as `N/A` and
serialized as `null` in JSON -- never fabricated as `0`.

```bash
python tests/report.py --sql                          # SQLite crawl-state database
python tests/report.py --redis                        # Redis frontier (config.yaml's namespace)
python tests/report.py --redis --namespace mycrawl     # a different Redis namespace
python tests/report.py --redis --output results/report.json
```

**Ad-hoc snapshot limitation:** the Redis frontier does not persist
run-level start/end timestamps or per-run parameters (query, seed files,
worker count, ...) -- per-URL metadata is deleted once a URL reaches a
terminal state (see `core/redis_frontier.py`'s `terminal_meta_ttl_seconds`).
An ad-hoc `--redis` report therefore shows accurate *current* URL-state
counts but `N/A` timing/throughput/run-identity, with a note explaining why.
The SQLite backend's `--sql` report can compute an approximate duration
from `MIN(first_seen)`/`MAX(last_seen)` in `storage/crawl_state.db`, since
that table is durable (unlike Redis's per-URL metadata).

### Overnight / monitored runs

For a run whose report should include real timing, resource usage, and
Redis stats, pass `--monitor-resources` (and optionally `--output`) to
`main.py` itself. This wraps the existing `manager.run()` call with the
same `ResourceMonitor` the benchmark suite uses
(`tests/benchmarks/common.py`) -- a lightweight background-thread sampler,
default interval 10s -- and reconnects to the backend after the crawl
finishes to build the final report from real process start/end timestamps:

```bash
python main.py --query "example query" --monitor-resources --monitor-interval 10 \
    --output results/overnight.json
```

Passing neither flag runs exactly as before (zero added overhead: no
monitor thread, no post-run reconnect). The JSON result's schema is
documented in `tests/report_lib.py`; `tests/report.py --run-json
results/overnight.json` reloads a captured run's metadata/timing/resources
into a fresh report (optionally combined with a live `--redis`/`--sql`
counts refresh).

## How to run Redis mode vs. SQL mode

Set `crawler.frontier.type` and `crawler.media_evidence.type` in
`config.yaml` to `"redis"` or `"sqlite"` (both currently ship commented
to `"redis"` as the default; toggle by editing the file — there is no CLI
flag to switch frontier backend at runtime, though `--media-backend`
*does* let you override the media evidence backend per-run). Redis mode
requires a reachable Redis server; SQLite mode needs nothing beyond local
disk.

## How to add a crawler engine

1. Add a new file in `crawler/`, implementing the same shape as an
   existing engine (e.g. `crawler/http_crawler.py`) — there is no shared
   base class to subclass, so read a comparable existing engine closely,
   particularly its `worker()` loop: claim via `AsyncFrontier`, wrap the
   fetch in `run_with_heartbeat`, and call `mark_visited`/`mark_failed`/
   `mark_skipped` on every exit path, including `asyncio.CancelledError`.
2. Wire it into `CrawlerManager.__init__`'s engine-selection branch and
   add the new choice to `main.py`'s `--crawler-engine` `choices=[...]`.
3. If it should participate in `HybridCrawler`'s escalation chain, wire it
   into `core/crawler_router.py`.
4. Add tests following the pattern in `tests/extra_crawlers_test.py` or
   `tests/hybrid_crawler_test.py`.

## How to add an extractor

Add a new module in `parsers/`, call it from the appropriate crawler
engine's post-fetch step (see how `HTMLLinkExtractor` and
`MediaLinkDetector` are invoked in an existing engine), and add a test
under `tests/parser_test.py` or a new dedicated test file.

## How to modify frontier behavior

Read [`docs/architecture/frontier-adr.md`](architecture/frontier-adr.md)
first — it's the canonical design vocabulary (states, keyspace shape,
claim/lease principles) both backends must stay consistent with. Any
change to `core/frontier.py`'s `Frontier` protocol must be implemented
identically in both `core/url_frontier.py` and `core/redis_frontier.py`,
and any change to `RedisURLFrontier`'s Lua scripts should be benchmarked
(`tests/benchmarks/frontier_benchmark.py --frontier redis`) before and
after, since prior work has already found and fixed one severe
accidental-latency regression here (see
[`docs/architecture/history/frontier-optimization-audit.md`](architecture/history/frontier-optimization-audit.md)).

## How to modify Media Evidence

Read [`media-evidence-redis-design.md`](architecture/media-evidence-redis-design.md)'s
"Architecture Boundaries" section first — it is explicitly called out in
that document as the single most load-bearing section; any change must
stay consistent with it (Redis is the sole production backend; no
SQL↔Redis synchronization in either direction; fingerprinting algorithms
live outside this subsystem entirely). Then read
[`media-evidence-step1.md`](architecture/media-evidence-step1.md) for how
the implementation actually differs, in small ways, from that design.

## Coding conventions

These are conventions already followed in the current codebase, not
proposed rules:

- Explicit typing on public function signatures; avoid bare `Any`.
- Small, single-responsibility functions — e.g. `URLFrontier`/
  `RedisURLFrontier` split `mark_visited`/`mark_failed`/`mark_skipped`
  into thin wrappers around one shared `_complete(claim, outcome, ...)`.
- Dataclasses for structured values crossing an API boundary
  (`FrontierClaim`, `FingerprintJob`, `FingerprintResult`), frozen where
  the value shouldn't be mutated after construction.
- Explicit exception types for infra-failure signals
  (`FrontierUnavailable`, `MediaEvidenceUnavailable`,
  `ClaimLostError`) rather than sentinel return values — see
  [Error-handling conventions](#error-handling--distributed-systems-conventions)
  below.
- Module docstrings that state architectural boundaries explicitly when
  a module's design intent isn't obvious from its code alone (see
  `storage/sqlite_media_evidence_store.py`'s and
  `storage/redis_media_evidence_store.py`'s docstrings).

## Error-handling / distributed-systems conventions

- **No silent fallback across a documented backend boundary.** A Redis
  failure must raise (`FrontierUnavailable` / `MediaEvidenceUnavailable`),
  never resolve to a value indistinguishable from a legitimate empty/zero
  result.
- **Every distributed mutation is one atomic round trip.** New Redis
  operations should be a single Lua script, not multiple round trips with
  client-side "read, decide, write" logic — that reintroduces the exact
  race conditions claim/lease/CAS exists to prevent.
- **Server time, not client time**, for anything lease/expiry-related —
  use Redis's own `TIME` inside the Lua script.
- **Claim tokens are the only proof of ownership.** Don't add a code path
  that lets a caller complete or fail a claim it didn't obtain a token
  for.
- **Long-running work under a claim must heartbeat** — wrap it in
  `run_with_heartbeat`, don't invent a second heartbeat mechanism.
- **Tests are required for distributed behavior changes.** Anything
  touching claim/lease/recovery/retry semantics should include a test
  that exercises the actual race or failure mode, not just the happy
  path — see `tests/redis_sqlite_mirror_removal_test.py`'s
  `FailingURLDatabase` pattern, or `tests/media_evidence_multiprocess_test.py`'s
  real multi-process claim test (asserts `duplicate_claims == 0` across
  independent OS processes), as models to follow.
- **Comments explain WHY, not WHAT.** Don't add a comment restating what
  the next line does; do add one recording a non-obvious constraint (see
  `_ensure_blacklist_file_exists`'s existence-check-before-touch, added
  specifically because the naive version silently poisoned an mtime-based
  cache — [history/optimization_blacklist.md](architecture/history/optimization_blacklist.md)).
