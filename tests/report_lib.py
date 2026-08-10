"""Shared building blocks for `tests/report.py` and the optional
`main.py --monitor-resources --output ...` overnight-run report.

Not a pytest suite (see `tests/report_test.py` for that) -- this module is
plain library code, imported by both the ad-hoc CLI report tool and, later,
by `main.py` for the live-run JSON result file.

Source-of-truth policy (see docs/architecture/frontier-adr.md and
core/redis_frontier.py): every Redis count in this module comes from
`RedisURLFrontier.get_status_counts()` and other current, documented keys
(`urls:known`, `domains:active`, per-domain queues) -- never from the old
`urls:queued`/`urls:failed` keys the previous report used, which do not
exist in the current keyspace. A metric this module cannot derive from the
current implementation is reported as `None` (JSON) / "N/A" (text), never
silently coerced to `0`.
"""

from __future__ import annotations

import datetime
import sqlite3
from typing import Optional

NA = "N/A"

# Keys read directly off `RedisURLFrontier.get_status_counts()` -- kept as a
# constant so callers/tests can assert against it instead of a magic list.
REDIS_STATUS_COUNT_KEYS = (
    "queued",
    "inflight",
    "retry_scheduled",
    "visited",
    "skipped",
    "failed_permanent",
)


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return NA
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def fmt_pct(value, digits: int = 2) -> str:
    if value is None:
        return NA
    return f"{value:.{digits}f}%"


def fmt_val(value) -> str:
    if value is None or value == "":
        return NA
    return str(value)


def safe_pct(part, whole) -> Optional[float]:
    """`part / whole * 100`, or `None` if either operand is unavailable or
    `whole` is zero (never a fabricated 0.0)."""
    if part is None or whole is None or whole == 0:
        return None
    return (part / whole) * 100.0


def safe_rate(count, seconds) -> Optional[float]:
    """`count / seconds`, or `None` if either operand is unavailable or the
    duration is zero/negative -- guards the zero-duration-throughput bug the
    previous report was exposed to."""
    if count is None or seconds is None or seconds <= 0:
        return None
    return count / seconds


def parse_timestamp(value) -> Optional[datetime.datetime]:
    """Parse a timestamp stored by either backend. SQLite stores
    `datetime.now(timezone.utc).isoformat()`; older rows or manual entries
    might be `%Y-%m-%d %H:%M:%S`. Returns `None` (never raises) if the value
    is missing or unparsable."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# SQLite snapshot
# ---------------------------------------------------------------------------

def sqlite_snapshot(db_path: str) -> dict:
    """Read current `urls` table state from the SQLite crawl-state database.

    Status values written by the crawler (see storage/url_database.py and
    every `crawler/*_crawler.py` engine): 'queued' (added to frontier, not
    yet attempted), 'pending' (currently being fetched -- the closest SQLite
    analog to Redis's 'inflight'), 'visited'/'failed' (terminal), 'skipped'
    (blacklisted at crawl time). SQLite has no durable retry-vs-permanent
    distinction -- that bookkeeping lives only in the in-process frontier's
    memory (`core/url_frontier.py`'s `_retry_heap`) and is lost once the
    process exits, so `retry_scheduled` is always `None` here.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")
        raw_counts = {(status or "unknown"): count for status, count in cur.fetchall()}

        cur = conn.execute("SELECT COUNT(*), MIN(first_seen), MAX(last_seen) FROM urls")
        total, first_seen, last_seen = cur.fetchone()
    finally:
        conn.close()

    return {
        "backend": "sqlite",
        "available": True,
        "discovered_total": total or 0,
        "visited": raw_counts.get("visited", 0),
        "failed_permanent": raw_counts.get("failed", 0),
        "skipped": raw_counts.get("skipped", 0),
        "queued": raw_counts.get("queued", 0),
        "inflight": raw_counts.get("pending", 0),
        "retry_scheduled": None,
        "raw_status_counts": raw_counts,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "notes": [
            "SQLite's 'pending' status is the closest analog to Redis's 'inflight' "
            "(currently being fetched), but also covers any URL left mid-flight by a "
            "crashed process -- it is an approximation, not an exact match.",
            "retry_scheduled is unavailable for the SQLite backend: retry bookkeeping "
            "lives only in the in-process frontier's memory and is not persisted.",
        ],
    }


def sqlite_timing(snapshot: dict) -> dict:
    start = parse_timestamp(snapshot.get("first_seen"))
    end = parse_timestamp(snapshot.get("last_seen"))
    if start and end:
        duration = max((end - start).total_seconds(), 0.0)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_seconds": duration,
            "source": "sqlite-first-last-seen",
            "note": (
                "Earliest 'first_seen' / latest 'last_seen' timestamp across all rows in "
                "the urls table -- an approximation of process start/end, not a guaranteed "
                "exact match (first_seen is when a URL was first written to storage, which "
                "can lag slightly behind actual process start)."
            ),
        }
    return {
        "start": None,
        "end": None,
        "duration_seconds": None,
        "source": "unavailable",
        "note": "No URL rows with a parsable first_seen/last_seen timestamp were found.",
    }


def sqlite_piracy_stats(db_path: str, piracy_sites: list[str]) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        visited, failed, queued = {}, {}, {}
        for site in piracy_sites:
            cur = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status = 'visited' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) "
                "FROM urls WHERE url LIKE ?",
                (f"%{site}%",),
            )
            v, f, q = cur.fetchone()
            visited[site] = v or 0
            failed[site] = f or 0
            queued[site] = q or 0
    finally:
        conn.close()
    return {"visited": visited, "failed": failed, "queued": queued}


# ---------------------------------------------------------------------------
# Redis snapshot
# ---------------------------------------------------------------------------

def redis_snapshot(
    host: str,
    port: int,
    db: int,
    namespace: str,
    frontier_kwargs: Optional[dict] = None,
) -> dict:
    """Snapshot current Redis frontier state using the production
    `RedisURLFrontier` class itself (never a hand-rolled key guess), so this
    can never drift from the actual keyspace the crawler uses.

    Returns `{"backend": "redis", "available": False, "error": ...}` on any
    connection/infrastructure failure -- callers must render that as
    "unavailable", never as all-zero counts.
    """
    import redis as redis_lib

    from core.frontier import FrontierUnavailable
    from core.redis_frontier import RedisURLFrontier

    frontier_kwargs = dict(frontier_kwargs or {})
    frontier_kwargs.pop("redis_host", None)
    frontier_kwargs.pop("redis_port", None)
    frontier_kwargs.pop("redis_db", None)
    frontier_kwargs.pop("namespace", None)

    try:
        frontier = RedisURLFrontier(
            redis_host=host, redis_port=port, redis_db=db, namespace=namespace, **frontier_kwargs
        )
    except redis_lib.RedisError as e:
        return {
            "backend": "redis",
            "available": False,
            "host": host, "port": port, "db": db, "namespace": namespace,
            "error": str(e),
        }

    try:
        try:
            counts = frontier.get_status_counts()
        except FrontierUnavailable as e:
            return {
                "backend": "redis",
                "available": False,
                "host": host, "port": port, "db": db, "namespace": namespace,
                "error": str(e),
            }

        conn = frontier.redis_conn
        known = conn.scard(frontier._key("urls", "known"))
        active_domains = conn.scard(frontier._key("domains", "active"))

        try:
            info = conn.info()
        except redis_lib.RedisError:
            info = {}

        return {
            "backend": "redis",
            "available": True,
            "host": host, "port": port, "db": db, "namespace": namespace,
            "discovered_total": known,
            "visited": counts["visited"],
            "queued": counts["queued"],
            "inflight": counts["inflight"],
            "retry_scheduled": counts["retry_scheduled"],
            "failed_permanent": counts["failed_permanent"],
            "skipped": counts["skipped"],
            "active_domains": active_domains,
            "redis_info": {
                "used_memory_mb": info.get("used_memory", 0) / (1024 * 1024) if info else None,
                "used_memory_peak_mb": (
                    info.get("used_memory_peak", 0) / (1024 * 1024) if info.get("used_memory_peak") else None
                ),
                "connected_clients": info.get("connected_clients"),
                "total_commands_processed": info.get("total_commands_processed"),
                "used_cpu_sys_seconds_total": info.get("used_cpu_sys"),
                "used_cpu_user_seconds_total": info.get("used_cpu_user"),
            },
        }
    finally:
        frontier.close()


def redis_timing_unavailable(namespace: str) -> dict:
    return {
        "start": None,
        "end": None,
        "duration_seconds": None,
        "source": "unavailable",
        "note": (
            f"The Redis frontier (namespace '{namespace}') does not persist run-level start/end "
            "timestamps. Per-URL 'first_seen' metadata (meta:{url}) is deleted as soon as a URL "
            "reaches a terminal state (visited/skipped/failed_permanent) unless "
            "terminal_meta_ttl_seconds is configured, so it cannot be used to reconstruct a "
            "finished run's timing after the fact. Run via "
            "`python main.py ... --monitor-resources --output <file>` to capture actual process "
            "start/end wall-clock timestamps for a run, or pass --run-json <file> to this report "
            "to merge in timing captured that way."
        ),
    }


def redis_piracy_stats(host: str, port: int, db: int, namespace: str, piracy_sites: list[str],
                        frontier_kwargs: Optional[dict] = None) -> dict:
    """Per-piracy-site breakdown, using only current, documented keys.

    `visited`/`failed_permanent` are real Redis SETs (cheap SMEMBERS).
    `queued` has no equivalent set (it's a derived count -- see
    `RedisURLFrontier.get_status_counts`), so it's reconstructed by walking
    `domains:active` and each domain's queue ZSET, which is exactly how the
    frontier itself finds queued work (bounded by active-domain count, never
    a keyspace SCAN).
    """
    import redis as redis_lib

    from core.redis_frontier import RedisURLFrontier

    frontier_kwargs = dict(frontier_kwargs or {})
    frontier_kwargs.pop("redis_host", None)
    frontier_kwargs.pop("redis_port", None)
    frontier_kwargs.pop("redis_db", None)
    frontier_kwargs.pop("namespace", None)

    try:
        frontier = RedisURLFrontier(
            redis_host=host, redis_port=port, redis_db=db, namespace=namespace, **frontier_kwargs
        )
    except redis_lib.RedisError:
        return {}

    try:
        conn = frontier.redis_conn
        visited = conn.smembers(frontier._key("urls", "visited"))
        failed = conn.smembers(frontier._key("urls", "failed_permanent"))

        queued: set[str] = set()
        active_domains = conn.smembers(frontier._key("domains", "active"))
        # Bounded by active-domain count (the same K-bound the claim path
        # itself uses), never a keyspace SCAN -- see class docstring.
        for domain in active_domains:
            queued.update(conn.zrange(frontier._key("domain", domain, "queue"), 0, -1))

        def counts_by_site(urls) -> dict:
            return {site: sum(1 for url in urls if site in url) for site in piracy_sites}

        return {
            "visited": counts_by_site(visited),
            "failed": counts_by_site(failed),
            "queued": counts_by_site(queued),
        }
    finally:
        frontier.close()


# ---------------------------------------------------------------------------
# Counts / throughput / failures assembly (backend-agnostic)
# ---------------------------------------------------------------------------

def counts_with_percentages(snapshot: dict) -> dict:
    total = snapshot.get("discovered_total")
    visited = snapshot.get("visited")
    failed = snapshot.get("failed_permanent")
    skipped = snapshot.get("skipped")

    return {
        "discovered_total": total,
        "visited": visited,
        "queued": snapshot.get("queued"),
        "inflight": snapshot.get("inflight"),
        "retry_scheduled": snapshot.get("retry_scheduled"),
        "failed_permanent": failed,
        "skipped": skipped,
        "percentages": {
            "visited_pct": safe_pct(visited, total),
            "failed_pct": safe_pct(failed, total),
            "skipped_pct": safe_pct(skipped, total),
        },
        "note": (
            "'discovered_total' is cumulative (every URL ever added, including ones now terminal); "
            "'queued' is current-state (URLs not yet attempted right now). They are not comparable "
            "1:1 and are never double-counted against each other."
        ),
    }


def compute_throughput(snapshot: dict, duration_seconds, worker_count=None) -> dict:
    discovered = snapshot.get("discovered_total")
    visited = snapshot.get("visited")
    failed = snapshot.get("failed_permanent") or 0
    skipped = snapshot.get("skipped") or 0
    completed = None
    if visited is not None:
        completed = visited + failed + skipped

    discovered_per_sec = safe_rate(discovered, duration_seconds)
    visited_per_sec = safe_rate(visited, duration_seconds)
    completed_per_sec = safe_rate(completed, duration_seconds)

    per_worker = None
    if completed_per_sec is not None and worker_count:
        per_worker = completed_per_sec / worker_count

    note = None
    if duration_seconds is None:
        note = "Duration is unavailable; throughput cannot be computed."
    elif duration_seconds <= 0:
        note = "Duration is zero or negative; throughput cannot be computed (avoids a divide-by-zero/inflated rate)."

    return {
        "discovered_per_sec": discovered_per_sec,
        "visited_per_sec": visited_per_sec,
        "completed_per_sec": completed_per_sec,
        "avg_completed_per_sec_per_worker": per_worker,
        "note": note,
        "per_worker_note": (
            "Simple average (completed/sec ÷ worker count); does not imply linear scaling across workers."
            if per_worker is not None else None
        ),
    }


def build_failures(snapshot: dict) -> dict:
    return {
        "successful_visits": snapshot.get("visited"),
        "permanently_failed": snapshot.get("failed_permanent"),
        "skipped": snapshot.get("skipped"),
        "retry_scheduled_current": snapshot.get("retry_scheduled"),
        "extraction_failures": None,
        "http_failures": None,
        "fetch_failures": None,
        "note": (
            "The crawler records aggregate visited/failed/skipped outcomes only; it does not "
            "categorize failures by HTTP-status vs. fetch-exception vs. extraction-error, so those "
            "subcategory breakdowns are unavailable (reporting fabricated subcategories was ruled out)."
        ),
    }


# ---------------------------------------------------------------------------
# Resource / Redis-resource report sections (from ResourceMonitor samples)
# ---------------------------------------------------------------------------

def build_resource_report(samples: list[dict], interval: float) -> Optional[dict]:
    """Summarize `tests.benchmarks.common.ResourceMonitor` samples into the
    process CPU/RSS section. Returns `None` if monitoring wasn't run."""
    if not samples:
        return None

    cpu = [s["process_cpu_percent"] for s in samples if s.get("process_cpu_percent") is not None]
    rss = [s["process_rss_mb"] for s in samples if s.get("process_rss_mb") is not None]

    return {
        "sample_count": len(samples),
        "sample_interval_seconds": interval,
        "process_cpu_percent_avg": (sum(cpu) / len(cpu)) if cpu else None,
        "process_cpu_percent_peak": max(cpu) if cpu else None,
        "process_rss_mb_avg": (sum(rss) / len(rss)) if rss else None,
        "process_rss_mb_peak": max(rss) if rss else None,
        "note": (
            "process_cpu_percent is instantaneous CPU utilization (%) sampled via psutil, not "
            "cumulative CPU time. Single crawler process (asyncio concurrency, not multiprocess) -- "
            "see metadata.worker_count for the number of concurrent async fetch tasks within it."
        ),
    }


def build_redis_resource_report(samples: list[dict], interval: float) -> Optional[dict]:
    """Summarize the Redis INFO fields `ResourceMonitor` already samples.
    Returns `None` if the monitor wasn't attached to a Redis connection."""
    mem = [s["redis_used_memory_mb"] for s in samples if s.get("redis_used_memory_mb") is not None]
    clients = [s["redis_connected_clients"] for s in samples if s.get("redis_connected_clients") is not None]
    cpu_sys = [s["redis_used_cpu_sys"] for s in samples if s.get("redis_used_cpu_sys") is not None]
    cpu_user = [s["redis_used_cpu_user"] for s in samples if s.get("redis_used_cpu_user") is not None]

    if not mem and not cpu_sys and not clients:
        return None

    return {
        "sample_count": len(samples),
        "sample_interval_seconds": interval,
        "memory_start_mb": mem[0] if mem else None,
        "memory_end_mb": mem[-1] if mem else None,
        "memory_delta_mb": (mem[-1] - mem[0]) if len(mem) >= 2 else None,
        "memory_peak_sampled_mb": max(mem) if mem else None,
        "connected_clients_avg": (sum(clients) / len(clients)) if clients else None,
        "connected_clients_peak": max(clients) if clients else None,
        "cpu_sys_seconds_delta": (cpu_sys[-1] - cpu_sys[0]) if len(cpu_sys) >= 2 else None,
        "cpu_user_seconds_delta": (cpu_user[-1] - cpu_user[0]) if len(cpu_user) >= 2 else None,
        "note": (
            "memory_peak_sampled_mb is the highest 'used_memory' observed at our sampling interval "
            "(not Redis's own lifetime used_memory_peak counter -- see counts.redis_info in a "
            "same-run snapshot for that). cpu_sys_seconds_delta/cpu_user_seconds_delta are deltas of "
            "Redis INFO's cumulative CPU-TIME counters (seconds consumed), not a CPU percentage -- "
            "conflating the two previously mis-attributed a benchmark bottleneck (see docs/benchmarks.md, "
            "'Resource measurements')."
        ),
    }


# ---------------------------------------------------------------------------
# Top-level report assembly
# ---------------------------------------------------------------------------

def build_report(*, metadata: dict, timing: dict, snapshot: dict, resources=None,
                  redis_resources=None, configuration=None) -> dict:
    counts = counts_with_percentages(snapshot)
    throughput = compute_throughput(snapshot, timing.get("duration_seconds"), metadata.get("worker_count"))
    failures = build_failures(snapshot)

    return {
        "metadata": metadata,
        "timing": timing,
        "counts": counts,
        "throughput": throughput,
        "failures": failures,
        "resources": resources,
        "redis": redis_resources,
        "configuration": configuration or {},
    }


# ---------------------------------------------------------------------------
# Human-readable terminal report
# ---------------------------------------------------------------------------

def render_human_report(report: dict) -> str:
    md = report["metadata"]
    timing = report["timing"]
    counts = report["counts"]
    throughput = report["throughput"]
    resources = report.get("resources")
    redis_res = report.get("redis")
    failures = report["failures"]

    lines: list[str] = []
    w = lines.append

    w("=" * 60)
    w("CRAWL RUN SUMMARY")
    w("=" * 60)
    w("")
    w(f"Backend:        {fmt_val(md.get('backend'))}")
    if md.get("redis"):
        r = md["redis"]
        w(f"Redis:          {r.get('host')}:{r.get('port')}/{r.get('db')} ns={r.get('namespace')}")
    if md.get("sqlite_path"):
        w(f"SQLite path:    {md.get('sqlite_path')}")
    w(f"Workers:        {fmt_val(md.get('worker_count'))}")
    w(f"Crawl engine:   {fmt_val(md.get('crawl_engine'))}")
    w(f"Search engines: {fmt_val(md.get('search_engines'))}")
    w(f"Web scope:      {fmt_val(md.get('query_scope'))}")
    w(f"Query:          {fmt_val(md.get('queries'))}")
    w(f"Seed files:     {fmt_val(md.get('seed_files'))}")
    w(f"Crawl mode:     {fmt_val(md.get('crawl_mode'))}")
    w(f"Max pages:      {fmt_val(md.get('max_pages'))}")
    w(f"Indefinite run: {fmt_val(md.get('indefinite_run'))}")
    w(f"Rate limit:     {fmt_val(md.get('rate_limit'))}")
    w("")
    w(f"Start:          {fmt_val(timing.get('start'))}")
    w(f"End:            {fmt_val(timing.get('end'))}")
    duration = timing.get("duration_seconds")
    w(f"Duration:       {fmt_num(duration)}s" if duration is not None else "Duration:       N/A")
    w(f"Timing source:  {fmt_val(timing.get('source'))}")
    if timing.get("note"):
        w(f"                ({timing['note']})")

    w("")
    w("---------------- URL STATE ----------------")
    w("")
    w(f"Discovered:        {fmt_val(counts.get('discovered_total'))}")
    w(f"Visited:           {fmt_val(counts.get('visited'))}  ({fmt_pct(counts['percentages'].get('visited_pct'))})")
    w(f"Queued:            {fmt_val(counts.get('queued'))}")
    w(f"In-flight:         {fmt_val(counts.get('inflight'))}")
    w(f"Retry scheduled:   {fmt_val(counts.get('retry_scheduled'))}")
    w(f"Permanently failed:{fmt_val(counts.get('failed_permanent'))}  ({fmt_pct(counts['percentages'].get('failed_pct'))})")
    w(f"Skipped:           {fmt_val(counts.get('skipped'))}  ({fmt_pct(counts['percentages'].get('skipped_pct'))})")

    w("")
    w("---------------- THROUGHPUT ----------------")
    w("")
    w(f"Discovered/sec: {fmt_num(throughput.get('discovered_per_sec'))}")
    w(f"Visited/sec:    {fmt_num(throughput.get('visited_per_sec'))}")
    w(f"Completed/sec:  {fmt_num(throughput.get('completed_per_sec'))}")
    if throughput.get("avg_completed_per_sec_per_worker") is not None:
        w(f"Avg/worker:     {fmt_num(throughput.get('avg_completed_per_sec_per_worker'))}")
    if throughput.get("note"):
        w(f"                ({throughput['note']})")

    w("")
    w("---------------- RESOURCES ----------------")
    w("")
    if resources:
        w(f"CPU avg:  {fmt_num(resources.get('process_cpu_percent_avg'))}%")
        w(f"CPU peak: {fmt_num(resources.get('process_cpu_percent_peak'))}%")
        w(f"RSS avg:  {fmt_num(resources.get('process_rss_mb_avg'))} MB")
        w(f"RSS peak: {fmt_num(resources.get('process_rss_mb_peak'))} MB")
        w(f"Samples:  {resources.get('sample_count')} (every {resources.get('sample_interval_seconds')}s)")
    else:
        w("Not monitored for this report (pass --monitor-resources to main.py during a run).")

    w("")
    w("---------------- REDIS ----------------")
    w("")
    if redis_res:
        w(f"Memory start: {fmt_num(redis_res.get('memory_start_mb'))} MB")
        w(f"Memory end:   {fmt_num(redis_res.get('memory_end_mb'))} MB")
        w(f"Memory delta: {fmt_num(redis_res.get('memory_delta_mb'))} MB")
        w(f"Memory peak:  {fmt_num(redis_res.get('memory_peak_sampled_mb'))} MB (sampled)")
        w(f"Clients avg/peak: {fmt_num(redis_res.get('connected_clients_avg'))} / {fmt_val(redis_res.get('connected_clients_peak'))}")
        w(f"CPU sys (Δ, seconds of CPU time): {fmt_num(redis_res.get('cpu_sys_seconds_delta'))}")
        w(f"CPU user (Δ, seconds of CPU time): {fmt_num(redis_res.get('cpu_user_seconds_delta'))}")
    elif report["metadata"].get("backend") == "redis":
        redis_info = report.get("snapshot_redis_info")
        if redis_info:
            w(f"Memory (current):   {fmt_num(redis_info.get('used_memory_mb'))} MB")
            w(f"Memory peak (life): {fmt_num(redis_info.get('used_memory_peak_mb'))} MB")
            w(f"Connected clients:  {fmt_val(redis_info.get('connected_clients'))}")
            w(f"Commands processed: {fmt_val(redis_info.get('total_commands_processed'))}")
            w("(Point-in-time snapshot only; not monitored -- pass --monitor-resources to main.py during a run for start/end/peak/delta.)")
        else:
            w("Not monitored for this report and no snapshot available.")
    else:
        w("N/A (not a Redis-backed run).")

    w("")
    w("---------------- ERRORS ----------------")
    w("")
    w(f"Successful visits:      {fmt_val(failures.get('successful_visits'))}")
    w(f"Permanently failed:     {fmt_val(failures.get('permanently_failed'))}")
    w(f"Skipped:                {fmt_val(failures.get('skipped'))}")
    w(f"Retry scheduled (now):  {fmt_val(failures.get('retry_scheduled_current'))}")
    w(f"Extraction failures:    {fmt_val(failures.get('extraction_failures'))}")
    w(f"HTTP failures:          {fmt_val(failures.get('http_failures'))}")
    w(f"Fetch failures:         {fmt_val(failures.get('fetch_failures'))}")
    if failures.get("note"):
        w(f"({failures['note']})")

    w("")
    w("=" * 60)

    return "\n".join(lines)
