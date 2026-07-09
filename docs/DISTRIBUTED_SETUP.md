# Distributed Crawler Setup Guide

This guide explains how to scale the anti-piracy crawler across multiple machines (2, 4, or more workers) using a shared Redis-backed frontier.

## Architecture Overview

```
┌─────────────────┐
│   Redis Server  │  ← Central coordinator (shared URL frontier)
│  (localhost:    │
│   6379)         │
└────────┬────────┘
         │
    ┌────┼────┬────────┬────────┐
    │    │    │        │        │
┌───▼──┐│┌─▼─▼──┐ ┌──▼───┐ ┌─▼────┐
│Work 1││Work 2 │ │Work 3│ │Work 4│
│Machine││Machine│ │Machine
│  A    ││  A    │ │  B   │ │ B   │
└───────┘└───────┘ └──────┘ └──────┘
```

Each worker:
- Connects to the shared Redis server
- Pulls URLs from the common frontier
- Updates URL status back to Redis
- Can be on a different machine or same machine

---

## Prerequisites

- Python 3.12+
- Redis server running
- All workers have cloned the same repository

---

## Step 1: Install and Start Redis

### On Linux (Ubuntu/Debian)

```bash
sudo apt install redis-server
sudo systemctl start redis-server
sudo systemctl enable redis-server  # Auto-start on boot
```

### On macOS (Homebrew)

```bash
brew install redis
brew services start redis
```

### On Windows (Docker recommended)

```bash
docker run -d -p 6379:6379 redis:latest
```

### Verify Redis is running

```bash
redis-cli ping
# Should respond with: PONG
```

---

## Step 2: Configure the Crawler for Redis

### Option A: Update `config.yaml` (Recommended for all workers)

Edit `config.yaml` in the crawler directory:

```yaml
crawler:
  engine: "auto"
  concurrency: 25
  timeout: 15
  max_pages: null  # Infinite for distributed mode
  rate_limit: 0.3
  user_agent: "AntiPiracyBot/1.0"
  storage:
    sqlite_path: "storage/crawl_state.db"
    media_sqlite_path: "storage/media_evidence.db"
    enable_media_evidence: true
  frontier:
    type: "redis"                    # ← Enable Redis frontier
    redis_host: "redis_server_ip"    # Replace with actual Redis IP
    redis_port: 6379
    redis_db: 0
    redis_namespace: "crawler"       # All workers use same namespace

search:
  enabled_engines:
    - "duckduckgo"
    - "bing"
    - "brave"
    - "yandex"
    - "ahmia"
    - "torch"
```

### Option B: Command-line Override (per run)

If all workers use the same machine for Redis:

```bash
# No changes needed to config.yaml - defaults work
python main.py
```

If Redis is on a different machine, update the host in config.yaml before running.

---

## Step 3: Prepare All Worker Machines

On each machine where you'll run a worker:

1. **Clone the repository** (or pull latest)

```bash
git clone https://github.com/DonDevil/crawler.git
cd crawler
```

2. **Create and activate virtual environment**

```bash
python3 -m venv env
source env/bin/activate
```

3. **Install dependencies**

```bash
pip install -r requirements.txt
```

4. **Update config.yaml** with your Redis server IP

---

## Step 4: Start Workers

Start one worker on each machine (or multiple on same machine).

### Machine A - Worker 1

```bash
source env/bin/activate
python main.py --indefinite-run
```

### Machine A - Worker 2 (optional, same machine)

In a new terminal:

```bash
source env/bin/activate
python main.py --indefinite-run
```

### Machine B - Worker 3

```bash
source env/bin/activate
python main.py --indefinite-run
```

### Machine B - Worker 4 (optional)

```bash
source env/bin/activate
python main.py --indefinite-run
```

---

## Step 5: Seed Initial URLs

On any machine, run once to populate the shared frontier:

```bash
source env/bin/activate
python main.py --clear-db  # Only on first run
python main.py --seed-file seeds/piracy_sites.txt --max-pages 100
```

This loads seed URLs into Redis. All workers will then pick up these URLs.

---

## Step 6: Monitor Progress

### Check Redis frontier status

```bash
redis-cli
> KEYS crawler:urls:*
> SCARD crawler:urls:queued
> SCARD crawler:urls:visited
```

### Check logs from workers

Each worker logs to stdout. Look for:
- `Using Redis frontier at ...` (confirms Redis connection)
- `Added to frontier: ...` (URLs being queued)
- `Database status counts:` (final stats)

### Monitor real-time via logs

```bash
# Terminal on each worker machine
tail -f crawler.log
```

---

## Step 7: Scaling Guidelines

### Adding more workers

Just start a new worker on any machine with the same config:

```bash
python main.py --indefinite-run
```

### For maximum throughput (4+ workers)

- **Per-domain rate limiting**: Configured in `core/redis_frontier.py` (default: 0.3s between requests to same domain)
- **Concurrency**: Each worker has `concurrency: 25` in config.yaml
- **Total effective throughput**: `num_workers × 25 requests` with domain politeness

Example with 4 workers:
- ~100 concurrent requests across all workers
- Per-domain rate limit still enforced (respects robots.txt conceptually)
- Typical discovery rate: 500-1000 URLs/hour shared across workers

### For stability (2-3 workers)

Use same config, but start only 2-3 workers to prevent overwhelming target sites.

---

## Step 8: Managing Frontier State

### Clear all URLs (fresh start)

```bash
redis-cli
> FLUSHDB
```

Or via code:

```bash
python -c "
from core.redis_frontier import RedisURLFrontier
frontier = RedisURLFrontier()
frontier.clear()
frontier.close()
print('Cleared')
"
```

### Check visited count

```bash
redis-cli
> SCARD crawler:urls:visited
```

### Export progress (optional)

```bash
redis-cli
> BGSAVE
# File saved to /var/lib/redis/dump.rdb
```

---

## Troubleshooting

### Error: "Connection refused"

- Check Redis is running: `redis-cli ping`
- Verify correct IP in config.yaml: `redis_host`
- Check firewall isn't blocking port 6379

### Error: "READONLY"

- Redis may be in readonly mode (replication issue)
- Solution: Restart Redis

### Workers not picking up URLs

- Verify all workers have `type: "redis"` in config
- Check Redis namespace is same for all workers
- Confirm seed URLs were added: `redis-cli SCARD crawler:urls:queued`

### Slow performance with multiple workers

- Check network latency to Redis (should be <5ms)
- Increase `concurrency` in config.yaml if not maxed out
- Monitor Redis CPU: `redis-cli INFO stats`

---

## Cleanup & Shutdown

### Stop workers gracefully

Press `Ctrl+C` on each worker terminal.

### Archive database

```bash
# Backup crawled URLs
redis-cli BGSAVE
cp /var/lib/redis/dump.rdb ./backup_$(date +%s).rdb
```

### Clear frontier for next run

```bash
redis-cli FLUSHDB
```

---

## Performance Expectations

| Configuration | Typical Rate | Notes |
|---|---|---|
| 1 worker, SQLite | 50-100 URLs/hour | Single machine, baseline |
| 2 workers, Redis | 200-300 URLs/hour | 2x throughput |
| 4 workers, Redis | 400-600 URLs/hour | 4-6x throughput |
| 4+ workers, Redis | 500-800 URLs/hour | Network or target site limits |

Actual numbers depend on:
- Network speed to target sites
- Target site response time
- Concurrency setting
- Rate limit aggressiveness

---

## Advanced Configuration

### Multiple Redis databases

If you want separate crawl sessions:

```yaml
frontier:
  type: "redis"
  redis_db: 1    # Use DB 1 instead of 0
```

### Custom namespace (for parallel experiments)

```yaml
frontier:
  type: "redis"
  redis_namespace: "crawler_experiment_2"
```

### Reduce rate limiting for faster crawls (use caution!)

```yaml
crawler:
  rate_limit: 0.1  # Down from default 0.3
```

---

## Next Steps

1. Start with 2 workers to validate setup
2. Scale to 4+ once stable
3. Monitor logs for errors
4. Adjust concurrency/rate_limit based on performance
