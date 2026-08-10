# Installation

Practical setup guide for a new developer. Every command below is verified
against the current `requirements.txt`, `config.yaml`, and `main.py`
argparse definitions — if a command here stops working, the docs are
wrong, not the repo; please fix this file.

## Requirements

- **Python 3.12** (confirmed via the project's own virtualenv,
  `env/lib/python3.12/`; there is no `pyproject.toml`/`.python-version`
  pinning this explicitly, so treat 3.12 as the target).
- **Redis**, only if you intend to run distributed/production mode
  (`frontier.type: "redis"` and/or `media_evidence.type: "redis"` in
  `config.yaml`). Not required for local SQLite-mode development.
- **Playwright and Selenium browser binaries**, only if you'll use
  `--crawler-engine playwright` or `--crawler-engine selenium` (or `auto`
  mode, which may escalate to them). `python -m playwright install` is
  required after `pip install` for the Playwright engine to work.
- **A running Tor SOCKS proxy**, only for `--crawler-engine tor` or dark-web
  crawling via `HybridCrawler`'s escalation. `tor/proxy_config.py` assumes
  a daemon is already reachable (default `127.0.0.1:9050`/`9150`) — this
  repository does not launch Tor itself.
- **No PostgreSQL, no FFmpeg.** `psycopg2-binary` appears in
  `requirements.txt` but is not imported anywhere in this codebase — it's
  leftover, not a real dependency; there is no PostgreSQL code in this
  repository. FFmpeg belongs to the separate, not-yet-built fingerprinter
  project, not to the crawler.

## Environment setup

```bash
git clone <this-repository-url>
cd crawler

python3.12 -m venv env
source env/bin/activate

pip install -r requirements.txt
```

`requirements.txt` is unpinned (no version constraints on any package).

If you'll use the Playwright engine:

```bash
python -m playwright install
```

## Python environment

This is the crawler's own environment only. The separate fingerprinter
project (future work, not present in this repository) has its own,
independent Python environment for its media-processing dependencies
(DINOv2, audio fingerprinting, FFmpeg bindings, etc.) — none of that
belongs in this environment, and none of it is required to run the
crawler.

## Redis

### Starting Redis

```bash
# Debian/Ubuntu
sudo apt install redis-server
sudo systemctl start redis-server

# macOS
brew install redis
brew services start redis

# Docker (any platform)
docker run -d --name crawler-redis -p 6379:6379 redis:latest
```

### Verifying Redis

```bash
redis-cli ping
# expect: PONG
```

### Namespaces and database numbers

The crawler uses **two independent Redis namespaces on the same physical
instance by default**, not two separate Redis servers:

| Use | `redis_db` | Namespace |
|---|---|---|
| Production frontier | 0 | `crawler` |
| Production media evidence | 0 | `evidence` |
| `tests/redis_frontier_test.py` | 1 | `test_crawler` |
| Other Redis-dependent pytest tests | 2 | test-specific |
| Benchmark scripts (`tests/benchmarks/*.py`) | 2 | `bench*` prefix |

Each is configured independently in `config.yaml` under
`crawler.frontier` and `crawler.media_evidence` respectively — they can
point at different hosts/ports/DBs if you want physical separation, but
by default they share one Redis instance and stay isolated by namespace
prefix alone.

### Development vs. production

For local development, SQLite mode needs no Redis at all — see
[Configuration](#configuration) below. For anything resembling production
(more than one crawler machine, or persistence across restarts that
matters), use Redis mode. **A Redis outage in Redis mode is a visible
failure, not a silent fallback to SQLite** — don't expect the crawler to
keep working locally if Redis goes down mid-run; that's deliberate, see
[system-architecture.md §22](architecture/system-architecture.md#22-failure-semantics).

## Configuration

`config.yaml` at the repository root, structure (defaults as shipped):

```yaml
crawler:
  engine: "auto"              # auto | async | http | tor | playwright | selenium | scrapling
  concurrency: 25
  timeout: 15
  max_pages: 200
  rate_limit: 0.3
  user_agent: "AntiPiracyBot/1.0"
  scrapling_enabled: true
  scrapling_headless: true
  scrapling_stealth: true
  scrapling_network_idle: true
  frontier:
    type: "redis"              # or "sqlite" for local/dev mode
    redis_host: "localhost"
    redis_port: 6379
    redis_namespace: "crawler"
    max_retries: 3
    base_backoff: 5.0
    max_backoff: 300.0
    lease_ttl: 90.0
    recovery_enabled: true
    recovery_interval: 30.0
    reclaim_batch_size: 200
    domain_scan_limit: 250
    # heartbeat_interval: null   # auto-derives to lease_ttl / 3
  seed_files:
    - "seeds/piracy_sites.txt"
    - "seeds/torrent_sites.txt"
    - "seeds/streaming_sites.txt"
    - "seeds/darkweb_seeds.txt"
  storage:
    sqlite_path: "storage/crawl_state.db"
    media_sqlite_path: "storage/media_evidence.db"
    enable_media_evidence: true
    enqueue_media_jobs: true
  media_evidence:
    type: "redis"              # or "sqlite"
    redis_host: "localhost"
    redis_port: 6379
    redis_namespace: "evidence"
    max_observations_per_asset: 20
    max_variants_per_asset: 20
    fingerprint_lease_ttl: 900.0
    # fingerprint_heartbeat_interval: null
    max_retries: 2
    base_backoff: 5.0
    max_backoff: 300.0
    reclaim_batch_size: 200
    confirmed_match_stream_maxlen: 10000
search:
  enabled_engines: ["duckduckgo", "bing", "brave", "yandex", "ahmia", "torch"]
  max_results_per_engine: 20
  timeout: 15
  engine_priorities: {torch: 0, ahmia: 2, brave: 4, bing: 5, duckduckgo: 6, yandex: 7}
  onion_priority_boost: 2
  blocked_engine_cooldown_queries: 999
```

To run entirely locally without Redis, change both `type:` lines to
`"sqlite"`. If `config.yaml` is missing entirely, the crawler runs with
all-pydantic defaults (`core/config.py`) rather than failing.

## Running

All examples assume `source env/bin/activate` has been run.

**Basic crawl from configured seed files:**

```bash
python main.py
```

**Query-driven discovery** (adds search-engine results on top of seed files):

```bash
python main.py --query "movie title"
```

**Query discovery only, skipping seed files:**

```bash
python main.py --query-only --query "movie title"
```

**Restrict discovery to surface-web or dark-web engines:**

```bash
python main.py --query-only --surface-web --query "movie title"
python main.py --query-only --dark-web --query "movie title"
```

**Choose a crawler engine explicitly** (default is `auto`, which routes
per-URL via `HybridCrawler`):

```bash
python main.py --crawler-engine http
python main.py --crawler-engine playwright
python main.py --crawler-engine selenium
python main.py --crawler-engine tor
```

**Force Redis or SQLite media-evidence backend for one run**, overriding
`config.yaml`:

```bash
python main.py --media-backend redis
python main.py --media-backend sqlite
```

**Run without a page cap**, until the frontier is genuinely empty:

```bash
python main.py --indefinite-run
```

**Override the page cap for one run:**

```bash
python main.py --max-pages 500
```

**Resume a previous run** — loads queued/pending URLs from storage only,
skipping seed files and fresh discovery:

```bash
python main.py --unfinished
```

**Additional seed files** (in addition to those listed in `config.yaml`):

```bash
python main.py --seed-file seeds/my_extra_list.txt
```

**Clear stored SQLite crawl state before starting:**

```bash
python main.py --clear-db
```

**Ignore the domain blacklist** (`datasets/domain_blacklist.txt`) for one run:

```bash
python main.py --ignore-blacklist
```

**Debug logging:**

```bash
python main.py --debug
```

**Manually exercise the fingerprint-job queue** (an operator tool, not a
worker loop — see
[system-architecture.md §18](architecture/system-architecture.md#18-fingerprint-job-lifecycle)):

```bash
python main.py --claim-fingerprint-job --worker-name manual-check
python main.py --complete-fingerprint-job <ASSET_ID> --claim-token <TOKEN> \
    --decision confirmed --match-title "Example Title" --match-confidence 0.95
```

There is no `--redis` / `--sql` flag on `main.py` itself — frontier
backend selection is via `config.yaml`'s `crawler.frontier.type`, not a
CLI flag. (`tests/report.py`, a separate reporting tool, does have
`--sql`/`--redis` flags — see [`docs/REPORT_TOOL.md`](REPORT_TOOL.md).)
`--search-engines` is not a flag either; enabled engines come from
`config.yaml`'s `search.enabled_engines`, narrowed per-run only by
`--surface-web`/`--dark-web`.
