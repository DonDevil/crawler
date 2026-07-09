import sqlite3
import datetime
import argparse
import sys
import os

# Add parent directory to path so we can import from core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_sqlite_data():
    """Fetch analytics data from SQLite database."""
    conn = sqlite3.connect("storage/crawl_state.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*), SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), SUM(CASE WHEN status = 'visited' THEN 1 ELSE 0 END), SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) FROM urls"
    )
    row = cursor.fetchone()

    total_found = row[0] or 0
    total_failed = row[1] or 0
    total_visited = row[2] or 0
    total_queued = row[3] or 0

    # Get time range
    cursor.execute("SELECT MIN(first_seen), MAX(last_seen) FROM urls")
    first_seen, last_seen = cursor.fetchone()

    cursor.close()
    conn.close()

    return {
        "total_found": total_found,
        "total_failed": total_failed,
        "total_visited": total_visited,
        "total_queued": total_queued,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


def get_redis_data():
    """Fetch analytics data from Redis frontier."""
    try:
        import redis
        from core.config import load_config

        config = load_config("config.yaml")
        frontier_config = config.crawler.frontier

        r = redis.Redis(
            host=frontier_config.redis_host,
            port=frontier_config.redis_port,
            db=frontier_config.redis_db,
            decode_responses=True,
        )

        # Test connection
        r.ping()

        namespace = frontier_config.redis_namespace

        # Get frontier counts
        total_queued = r.scard(f"{namespace}:urls:queued") or 0
        total_visited = r.scard(f"{namespace}:urls:visited") or 0
        total_failed = r.scard(f"{namespace}:urls:failed") or 0
        total_skipped = r.scard(f"{namespace}:urls:skipped") or 0

        total_found = total_queued + total_visited + total_failed + total_skipped

        # Get first/last seen timestamps from metadata
        first_seen_key = f"{namespace}:metadata:first_crawl_time"
        last_seen_key = f"{namespace}:metadata:last_crawl_time"

        first_seen = r.get(first_seen_key)
        last_seen = r.get(last_seen_key)

        return {
            "total_found": total_found,
            "total_failed": total_failed,
            "total_visited": total_visited,
            "total_queued": total_queued,
            "total_skipped": total_skipped,
            "first_seen": first_seen,
            "last_seen": last_seen,
        }

    except Exception as e:
        print(f"Error connecting to Redis: {e}", file=sys.stderr)
        print("Make sure Redis is running and configured correctly.", file=sys.stderr)
        sys.exit(1)


def get_piracy_stats(data, source="sql"):
    """Calculate piracy site statistics."""
    try:
        with open("seeds/piracy_sites.txt", "r", encoding="utf-8") as f:
            piracy_sites = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        print("Warning: seeds/piracy_sites.txt not found, skipping piracy stats", file=sys.stderr)
        return {}

    if source == "sql":
        return get_piracy_stats_sql(piracy_sites)
    elif source == "redis":
        return get_piracy_stats_redis(piracy_sites)


def get_piracy_stats_sql(piracy_sites):
    """Get piracy site statistics from SQLite."""
    conn = sqlite3.connect("storage/crawl_state.db")
    cursor = conn.cursor()

    pirate_visited_counts = {}
    pirate_failed_counts = {}
    pirate_queued_counts = {}

    for site in piracy_sites:
        cursor.execute(
            "SELECT SUM(CASE WHEN status = 'visited' THEN 1 ELSE 0 END), SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) FROM urls WHERE url LIKE ?",
            (f"%{site}%",),
        )
        pirate_visited_count, pirate_failed_count, pirate_queued_count = cursor.fetchone()
        pirate_visited_counts[site] = pirate_visited_count or 0
        pirate_failed_counts[site] = pirate_failed_count or 0
        pirate_queued_counts[site] = pirate_queued_count or 0

    cursor.close()
    conn.close()

    return {
        "visited": pirate_visited_counts,
        "failed": pirate_failed_counts,
        "queued": pirate_queued_counts,
    }


def get_piracy_stats_redis(piracy_sites):
    """Get piracy site statistics from Redis."""
    try:
        import redis
        from core.config import load_config

        config = load_config("config.yaml")
        frontier_config = config.crawler.frontier

        r = redis.Redis(
            host=frontier_config.redis_host,
            port=frontier_config.redis_port,
            db=frontier_config.redis_db,
            decode_responses=True,
        )
        r.ping()

        namespace = frontier_config.redis_namespace

        # Get all URLs from frontier
        queued = r.smembers(f"{namespace}:urls:queued")
        visited = r.smembers(f"{namespace}:urls:visited")
        failed = r.smembers(f"{namespace}:urls:failed")

        pirate_visited_counts = {}
        pirate_failed_counts = {}
        pirate_queued_counts = {}

        for site in piracy_sites:
            pirate_visited_counts[site] = sum(1 for url in visited if site in url)
            pirate_failed_counts[site] = sum(1 for url in failed if site in url)
            pirate_queued_counts[site] = sum(1 for url in queued if site in url)

        return {
            "visited": pirate_visited_counts,
            "failed": pirate_failed_counts,
            "queued": pirate_queued_counts,
        }

    except Exception as e:
        print(f"Error getting piracy stats from Redis: {e}", file=sys.stderr)
        return {}


# Parse command-line arguments
parser = argparse.ArgumentParser(
    description="Analyze crawler database - SQLite or Redis frontier",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Examples:
  python report.py --sql       # Analyze SQLite database
  python report.py --redis     # Analyze Redis frontier
  python report.py             # Auto-detect (default: SQLite)
""",
)

parser.add_argument(
    "--sql",
    action="store_true",
    help="Analyze SQLite database (default if neither flag specified)",
)
parser.add_argument(
    "--redis",
    action="store_true",
    help="Analyze Redis frontier instead of SQLite",
)

args = parser.parse_args()

# Determine data source
if args.redis:
    source = "redis"
    data = get_redis_data()
elif args.sql or not args.redis:  # Default to SQL
    source = "sql"
    data = get_sqlite_data()

# Total Analytics

total_found = data["total_found"]
total_failed = data["total_failed"]
total_visited = data["total_visited"]
total_queued = data["total_queued"]

# Efficiency Metrics

total_attempted = total_visited + total_failed
successful_visit_rate = (total_visited / total_attempted) * 100 if total_attempted > 0 else 0
failure_rate = (total_failed / total_attempted) * 100 if total_attempted > 0 else 0
crawl_completion_rate = (total_visited / total_found) * 100 if total_found > 0 else 0
remaining_queue_share = (total_queued / total_found) * 100 if total_found > 0 else 0
discovery_pressure = total_attempted / total_found if total_found > 0 else 0

# Total Run Time Calculation

first_seen = data["first_seen"]
last_seen = data["last_seen"]

if first_seen and last_seen:
    try:
        start_time = datetime.datetime.fromisoformat(first_seen)
        end_time = datetime.datetime.fromisoformat(last_seen)
    except (ValueError, TypeError):
        try:
            start_time = datetime.datetime.strptime(first_seen, "%Y-%m-%d %H:%M:%S")
            end_time = datetime.datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            total_run_hours = 0
            start_time = None
            end_time = None
    
    if start_time and end_time:
        total_run_hours = max((end_time - start_time).total_seconds() / 3600.0, 0)
    else:
        total_run_hours = 0
else:
    total_run_hours = 0

# Per-Hour Metrics

total_attempted_per_hour = total_attempted / total_run_hours if total_run_hours > 0 else 0
successful_visit_per_hour = total_visited / total_run_hours if total_run_hours > 0 else 0
failure_per_hour = total_failed / total_run_hours if total_run_hours > 0 else 0

# Piracy Site Analytics

piracy_stats = get_piracy_stats(data, source=source)

if piracy_stats:
    pirate_visited_counts = piracy_stats.get("visited", {})
    pirate_failed_counts = piracy_stats.get("failed", {})
    pirate_queued_counts = piracy_stats.get("queued", {})

    total_visited_pirated = sum(pirate_visited_counts.values())
    total_failed_pirated = sum(pirate_failed_counts.values())
    total_queued_pirated = sum(pirate_queued_counts.values())

    visited_pirated_percentage = (total_visited_pirated / total_found) * 100 if total_found > 0 else 0
    failed_pirated_percentage = (total_failed_pirated / total_found) * 100 if total_found > 0 else 0
    queued_pirated_percentage = (total_queued_pirated / total_found) * 100 if total_found > 0 else 0
else:
    total_visited_pirated = 0
    total_failed_pirated = 0
    total_queued_pirated = 0
    visited_pirated_percentage = 0
    failed_pirated_percentage = 0
    queued_pirated_percentage = 0


# Close Connection (not needed for Redis, but kept for consistency)
# Already closed in data functions

# Display Summary

source_label = "Redis Frontier" if source == "redis" else "SQLite Database"
print(f"------------------------------------------Summary (from {source_label})------------------------------------------------")

print("#Total")

print("\n")

print(f"Total URLs Found: {total_found}")
print(f"Total URLs Visited: {total_visited}")
print(f"Total URLs Failed: {total_failed}")
print(f"Total URLs Queued: {total_queued}")
if source == "redis" and data.get("total_skipped"):
    print(f"Total URLs Skipped: {data['total_skipped']}")

print("\n")

print("#Efficiency Metrics")

print("\n")

print(f"Total Attempted: {total_attempted}")
print(f"Successful Visit Rate: {successful_visit_rate:.2f}%")
print(f"Failure Rate: {failure_rate:.2f}%")
print(f"Crawl Completion Rate: {crawl_completion_rate:.2f}%")
print(f"Remaining Queue Share: {remaining_queue_share:.2f}%")
print(f"Discovery Pressure: {discovery_pressure:.2f}")

print("\n")

print("#Per-Hour Metrics")

print("\n")

print(f"Total Run Hours: {total_run_hours:.2f} hours")
print(f"Total Attempted per Hour: {total_attempted_per_hour:.2f}")
print(f"Successful Visit per Hour: {successful_visit_per_hour:.2f}")
print(f"Failure per Hour: {failure_per_hour:.2f}")

print("\n")

print("#Piracy Site Analytics")

print("\n")

print(f"Total Visited Pirated Sites: {total_visited_pirated}")
print(f"Total Failed Pirated Sites: {total_failed_pirated}")
print(f"Total Queued Pirated Sites: {total_queued_pirated}")
print("\n")
print(f"Visited Pirated Sites Percentage: {visited_pirated_percentage:.2f}%")
print(f"Failed Pirated Sites Percentage: {failed_pirated_percentage:.2f}%")
print(f"Queued Pirated Sites Percentage: {queued_pirated_percentage:.2f}%")

