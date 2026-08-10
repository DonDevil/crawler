# Report Tool - SQLite & Redis Analysis

> **Known bug, not yet fixed (documentation only — this file does not
> modify code):** `tests/report.py --redis` queries `{namespace}:urls:queued`
> and `{namespace}:urls:failed` via `SCARD`. The current production
> frontier keyspace (`core/redis_frontier.py`) has no such keys — "queued"
> is derived arithmetically rather than stored as a literal set, and the
> terminal-failure set is named `{namespace}:urls:failed_permanent`, not
> `{namespace}:urls:failed`. As a result, `--redis` mode currently always
> reports `Total URLs Queued: 0` and `Total URLs Failed: 0` against a real
> production Redis frontier. `--sql`/default (SQLite) mode is accurate
> against the current `storage/url_database.py` schema. See
> [`../docs/development.md`](development.md#how-to-run-tests) and
> [`architecture/system-architecture.md` §25](architecture/system-architecture.md#25-current-limitations).

The `tests/report.py` script analyzes crawler performance metrics from either SQLite database or Redis frontier.

## Features

- **SQLite Analysis** – Query local SQLite database for comprehensive crawl statistics
- **Redis Analysis** – Query Redis frontier for real-time distributed crawler status
- **Piracy Site Tracking** – Analyze performance on known piracy domains
- **Efficiency Metrics** – Visit rate, failure rate, discovery pressure, etc.
- **Per-Hour Metrics** – Calculate throughput and progress rates

## Usage

### Default (SQLite)

```bash
python tests/report.py
```

Analyzes the local SQLite database (`storage/crawl_state.db`).

### Explicit SQLite

```bash
python tests/report.py --sql
```

Same as default, explicitly specifies SQLite source.

### Redis Frontier

```bash
python tests/report.py --redis
```

Connects to Redis server (configured in `config.yaml`) and analyzes frontier state.

## Output

Both modes produce identical report format with sections:

### #Total
- Total URLs Found
- Total URLs Visited
- Total URLs Failed
- Total URLs Queued
- (Redis only) Total URLs Skipped

### #Efficiency Metrics
- Total Attempted (visited + failed)
- Successful Visit Rate (%)
- Failure Rate (%)
- Crawl Completion Rate (%)
- Remaining Queue Share (%)
- Discovery Pressure (ratio)

### #Per-Hour Metrics
- Total Run Hours
- Total Attempted per Hour
- Successful Visit per Hour
- Failure per Hour

### #Piracy Site Analytics
- Total Visited Pirated Sites
- Total Failed Pirated Sites
- Total Queued Pirated Sites
- Percentages for each category

## Examples

### Single-Worker SQLite Analysis

```bash
$ python tests/report.py --sql

------------------------------------------Summary (from SQLite Database)--------
#Total

Total URLs Found: 38906
Total URLs Visited: 1309
Total URLs Failed: 170
Total URLs Queued: 37286

#Efficiency Metrics

Total Attempted: 1479
Successful Visit Rate: 88.51%
Failure Rate: 11.49%
...
```

### Multi-Worker Redis Analysis

```bash
$ python tests/report.py --redis

------------------------------------------Summary (from Redis Frontier)--------
#Total

Total URLs Found: 12345
Total URLs Visited: 4532
Total URLs Failed: 289
Total URLs Queued: 7524

#Efficiency Metrics

Total Attempted: 4821
Successful Visit Rate: 93.88%
...
```

### Compare Before/After Frontier Type Change

Compare SQLite baseline:
```bash
python tests/report.py --sql > report_baseline.txt
```

After switching to Redis and running multi-worker crawl:
```bash
python tests/report.py --redis > report_redis.txt
diff report_baseline.txt report_redis.txt
```

## Configuration

The `--redis` flag uses settings from `config.yaml`:

```yaml
crawler:
  frontier:
    type: "redis"
    redis_host: "localhost"     # Redis server IP/hostname
    redis_port: 6379           # Redis port
    redis_db: 0                # Redis database number
    redis_namespace: "crawler" # Namespace for URLs
```

Ensure Redis is running and accessible before using `--redis` flag.

## Error Handling

### Redis Not Available

If Redis is not running or unreachable:

```
Error connecting to Redis: No module named 'redis'
Make sure Redis is running and configured correctly.
```

Install redis-py: `pip install redis`

Or ensure Redis server is running:
```bash
redis-cli ping  # Should respond with PONG
```

### Missing Piracy Sites File

If `seeds/piracy_sites.txt` is missing:

```
Warning: seeds/piracy_sites.txt not found, skipping piracy stats
```

The report continues with core metrics; piracy analysis is skipped.

## Implementation Details

### SQLite Mode

- **Data Source**: `storage/crawl_state.db` (urls table)
- **Status Values**: queued, visited, failed, skipped
- **Time Tracking**: first_seen and last_seen timestamps
- **Piracy Matching**: SQL LIKE query on URL field

### Redis Mode

- **Data Source**: Redis sets and sorted sets (configured namespace)
- **Deduplication**: Uses `{namespace}:urls:visited`, `{namespace}:urls:queued`, `{namespace}:urls:failed`
  — **the last two do not exist in the current frontier keyspace** (see the
  known-bug notice at the top of this file); only `visited` reads
  correctly today.
- **Time Tracking**: Optional metadata keys `{namespace}:metadata:first_crawl_time` and `{namespace}:metadata:last_crawl_time`
- **Piracy Matching**: Iterates through set members, matches domain substring

## Performance Notes

### SQLite Mode
- Queries run against local database
- Fast for single-machine analysis
- Good for historical data (persists across restarts)

### Redis Mode
- Connects to Redis server (network I/O)
- Real-time frontier state
- Useful for monitoring active multi-worker crawls
- Namespace isolation supports concurrent crawlers

## Workflow Example

1. **Start single-worker baseline run**
   ```bash
   python main.py --seed-file seeds/piracy_sites.txt --max-pages 100
   python tests/report.py --sql  # Baseline metrics
   ```

2. **Switch to Redis + multi-worker**
   ```bash
   # Update config.yaml: frontier.type = "redis"
   redis-server &  # Start Redis
   python main.py --seed-file seeds/piracy_sites.txt --max-pages 100  # Seed frontier
   python main.py --indefinite-run &  # Worker 1
   python main.py --indefinite-run &  # Worker 2
   python main.py --indefinite-run &  # Worker 3
   python tests/report.py --redis  # Check progress
   ```

3. **Compare throughput**
   ```bash
   # Run both and compare per-hour metrics
   python tests/report.py --sql   > single_worker.txt
   python tests/report.py --redis > multi_worker.txt
   ```

---

**Updated**: 2026-07-09 (original), documentation corrected 2026-08-10.
**Tested**: SQLite mode verified working against the current schema.
Redis mode (`--redis`) has a known `queued`/`failed` reporting bug against
the current frontier keyspace — see the notice at the top of this file.
