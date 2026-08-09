"""Shared helpers for the Step 6 frontier validation/benchmark harness.

These are plain CLI scripts, not pytest tests -- deliberately not named
`*_test.py`/`test_*.py` so `pytest` never auto-collects them. Each script in
this directory bootstraps `sys.path` itself (repo root + this directory) so
it can be run directly with `python tests/benchmarks/<script>.py`, matching
how the rest of this repo's tests import `core`/`storage`/`utils`.

Nothing here touches frontier internals, Lua scripts, or crawler worker
logic -- this module only builds frontier instances via their public
constructors, generates synthetic workloads, and reports results.
"""

from __future__ import annotations

import atexit
import csv
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.redis_frontier import RedisURLFrontier  # noqa: E402
from core.url_frontier import URLFrontier  # noqa: E402
from utils.url_utils import URLUtils  # noqa: E402

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None


# Benchmark runs default to a namespace/db distinct from both production
# ("crawler") and the existing pytest Redis suite (db=1, namespace
# "test_crawler") so a benchmark run can never collide with either.
DEFAULT_REDIS_DB = 2
DEFAULT_NAMESPACE = "bench"


# ---------------------------------------------------------------------------
# Frontier construction
# ---------------------------------------------------------------------------

def build_frontier(
    kind: str,
    *,
    rate_limit: float = 1.0,
    max_retries: int = 3,
    base_backoff: float = 0.1,
    max_backoff: float = 1.0,
    lease_ttl: float = 90.0,
    redis_host: str = "localhost",
    redis_port: int = 6379,
    redis_db: int = DEFAULT_REDIS_DB,
    namespace: str = DEFAULT_NAMESPACE,
    domain_scan_limit: int = 50,
    reclaim_batch_size: int = 200,
):
    """Build a frontier via its normal public constructor -- no monkeypatching,
    no architecture changes. `kind` is 'local' or 'redis'."""

    if kind == "local":
        return URLFrontier(
            rate_limit=rate_limit,
            max_retries=max_retries,
            base_backoff=base_backoff,
            max_backoff=max_backoff,
            lease_ttl=lease_ttl,
        )
    if kind == "redis":
        return RedisURLFrontier(
            redis_host=redis_host,
            redis_port=redis_port,
            redis_db=redis_db,
            namespace=namespace,
            rate_limit=rate_limit,
            max_retries=max_retries,
            base_backoff=base_backoff,
            max_backoff=max_backoff,
            lease_ttl=lease_ttl,
            domain_scan_limit=domain_scan_limit,
            reclaim_batch_size=reclaim_batch_size,
        )
    raise ValueError(f"unknown frontier kind: {kind!r} (expected 'local' or 'redis')")


def add_common_frontier_args(
    parser,
    default_namespace: str = DEFAULT_NAMESPACE,
    default_rate_limit: float = 1.0,
    default_max_retries: int = 3,
    default_base_backoff: float = 0.1,
    default_max_backoff: float = 1.0,
    default_lease_ttl: float = 90.0,
) -> None:
    """Attach the CLI flags every script needs to build/connect a frontier.

    Defaults can be overridden per-script (e.g. the crash/recovery and
    heartbeat scripts want a short `--lease-ttl` for a fast, deterministic
    run) without redefining the flags themselves, which would conflict.
    """

    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-db", type=int, default=DEFAULT_REDIS_DB,
                         help="Redis DB for this benchmark run (default %(default)s; "
                              "the pytest Redis suite uses db=1, production uses db=0)")
    parser.add_argument("--namespace", default=default_namespace,
                         help="Redis key namespace for this run (default %(default)s)")
    parser.add_argument("--rate-limit", type=float, default=default_rate_limit,
                         help="Per-domain minimum seconds between claims")
    parser.add_argument("--max-retries", type=int, default=default_max_retries)
    parser.add_argument("--base-backoff", type=float, default=default_base_backoff)
    parser.add_argument("--max-backoff", type=float, default=default_max_backoff)
    parser.add_argument("--lease-ttl", type=float, default=default_lease_ttl)


def frontier_kwargs_from_args(args) -> dict:
    return dict(
        rate_limit=args.rate_limit,
        max_retries=args.max_retries,
        base_backoff=args.base_backoff,
        max_backoff=args.max_backoff,
        lease_ttl=args.lease_ttl,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        namespace=args.namespace,
    )


# ---------------------------------------------------------------------------
# Blacklist isolation
# ---------------------------------------------------------------------------
#
# `URLUtils.is_blacklisted()`/`clean_url()` (called by every `Frontier.add_url()`
# and `get_next_url()`) default to reading/writing the production
# `datasets/domain_blacklist.txt`. Synthetic benchmark URLs must never be
# checked against -- or ever able to pollute -- that file. See
# docs/architecture/benchmark_bug_audit.md for the incident this fixes.

def isolate_blacklist(directory: Optional[str] = None) -> Path:
    """Point `URLUtils` at a fresh, empty, temporary blacklist file for the
    remainder of this process. Must be called before any frontier workload
    is built (frontier construction itself doesn't touch the blacklist, but
    `add_url()`/`get_next_url()` do). Never touches the production
    `datasets/domain_blacklist.txt`.

    Returns the isolated file's path so it can be forwarded to worker
    processes that build their own frontier connection (multiprocessing's
    default fork start method on Linux would already inherit this via the
    `URLUtils` class attribute, but passing it explicitly doesn't rely on
    that and works under 'spawn' too).
    """

    fd, path_str = tempfile.mkstemp(prefix="bench_blacklist_", suffix=".txt", dir=directory)
    os.close(fd)
    path = Path(path_str)
    path.write_text("", encoding="utf-8")  # must start empty for every run
    URLUtils.set_blacklist_path(str(path))
    atexit.register(lambda: path.unlink(missing_ok=True))
    return path


# ---------------------------------------------------------------------------
# Synthetic workload generation
# ---------------------------------------------------------------------------

def make_domains(domain_count: int, prefix: str = "bench", run_id: str = "") -> list[str]:
    """`run_id`, when given, is embedded in every domain name so synthetic
    domains from one run can never collide with a stale blacklist entry (or
    leftover frontier state) from a previous run -- see
    docs/architecture/benchmark_bug_audit.md. Matches the per-run-unique
    naming approach `priority_ratelimit.py` already used."""

    tag = f"{prefix}-{run_id}" if run_id else prefix
    return [f"{tag}{i}.example.test" for i in range(domain_count)]


def parse_priority_distribution(spec: str) -> Callable[["random.Random"], int]:
    """Parse a priority-distribution spec into `fn(rng) -> priority`.

    Formats:
      "fixed:10"                       every URL gets priority 10
      "uniform:1-10"                   uniform random integer in [1, 10]
      "weighted:1:0.2,5:0.5,10:0.3"    priority:weight pairs (weights need not sum to 1)
    """

    spec = spec.strip()
    if spec.startswith("fixed:"):
        value = int(spec.split(":", 1)[1])
        return lambda rng: value

    if spec.startswith("uniform:"):
        lo_s, hi_s = spec.split(":", 1)[1].split("-")
        lo, hi = int(lo_s), int(hi_s)
        return lambda rng: rng.randint(lo, hi)

    if spec.startswith("weighted:"):
        pairs = spec.split(":", 1)[1].split(",")
        priorities = []
        weights = []
        for pair in pairs:
            p_s, w_s = pair.split(":")
            priorities.append(int(p_s))
            weights.append(float(w_s))
        return lambda rng: rng.choices(priorities, weights=weights, k=1)[0]

    raise ValueError(
        f"bad --priority-distribution {spec!r}; expected fixed:<p>, uniform:<lo>-<hi>, "
        "or weighted:<p1>:<w1>,<p2>:<w2>,..."
    )


def make_synthetic_urls(
    count: int,
    domain_count: int,
    priority_fn: Callable[["random.Random"], int],
    rng,
    run_id: str = "",
    domain_prefix: str = "bench",
) -> list[tuple[str, int]]:
    """Generate `count` synthetic (url, priority) pairs spread round-robin
    across `domain_count` distinct domains. `run_id` disambiguates both URLs
    and domain names across repeated runs (so a stale blacklist entry or
    leftover frontier state from a prior run can never collide with this
    one -- see docs/architecture/benchmark_bug_audit.md)."""

    domains = make_domains(domain_count, prefix=domain_prefix, run_id=run_id)
    urls = []
    for i in range(count):
        domain = domains[i % max(1, domain_count)]
        priority = priority_fn(rng)
        suffix = f"{run_id}-{i}" if run_id else str(i)
        urls.append((f"https://{domain}/synthetic/{suffix}", priority))
    return urls


# ---------------------------------------------------------------------------
# Latency / percentile stats
# ---------------------------------------------------------------------------

def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return ordered[int(k)]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def latency_stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min_s": min(values),
        "max_s": max(values),
        "mean_s": sum(values) / len(values),
        "p50_s": percentile(values, 50),
        "p90_s": percentile(values, 90),
        "p99_s": percentile(values, 99),
    }


# ---------------------------------------------------------------------------
# Resource monitoring (psutil process stats + Redis INFO, best-effort)
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """Background sampler for process CPU/RSS and Redis memory/CPU.

    Uses `psutil` if it's already installed (it is, transitively, in this
    repo's venv); degrades to process-only or empty stats otherwise. Never
    raises -- resource monitoring is best-effort and must never fail a
    benchmark run.
    """

    def __init__(self, redis_conn=None, interval: float = 0.5, include_children: bool = False):
        self.redis_conn = redis_conn
        self.interval = max(0.05, interval)
        self.include_children = include_children
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.samples: list[dict] = []
        self._proc = None
        self._child_procs: dict[int, "psutil.Process"] = {}
        if psutil is not None:
            try:
                self._proc = psutil.Process(os.getpid())
            except Exception:
                self._proc = None

    def _sample_children(self) -> tuple[int, float, float]:
        """psutil's cpu_percent(interval=None) is only meaningful across two
        calls on the *same* Process object with time elapsed between them --
        a freshly-constructed Process (which `.children()` returns every
        call) always reports 0.0 on its first call. Cache child Process
        objects by pid across samples so percentages are real deltas, and
        skip counting a child's first sighting (no prior baseline yet)."""

        try:
            current = self._proc.children(recursive=True)
        except Exception:
            return 0, 0.0, 0.0

        current_pids = {child.pid for child in current}
        for pid in list(self._child_procs):
            if pid not in current_pids:
                self._child_procs.pop(pid, None)

        cpu_total = 0.0
        rss_total = 0.0
        for child in current:
            cached = self._child_procs.get(child.pid)
            if cached is None:
                self._child_procs[child.pid] = child
                try:
                    child.cpu_percent(interval=None)  # prime baseline
                except Exception:
                    pass
                continue
            try:
                cpu_total += cached.cpu_percent(interval=None)
                rss_total += cached.memory_info().rss / (1024 * 1024)
            except Exception:
                continue

        return len(current_pids), cpu_total, rss_total

    def _sample_once(self) -> dict:
        sample: dict = {"t": time.time()}
        if self._proc is not None:
            try:
                sample["process_cpu_percent"] = self._proc.cpu_percent(interval=None)
                sample["process_rss_mb"] = self._proc.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
            if self.include_children:
                count, child_cpu, child_rss = self._sample_children()
                sample["child_process_count"] = count
                sample["children_cpu_percent"] = child_cpu
                sample["children_rss_mb"] = child_rss
                sample["total_cpu_percent"] = sample.get("process_cpu_percent", 0.0) + child_cpu
                sample["total_rss_mb"] = sample.get("process_rss_mb", 0.0) + child_rss
        if self.redis_conn is not None:
            try:
                info = self.redis_conn.info()
                sample["redis_used_memory_mb"] = info.get("used_memory", 0) / (1024 * 1024)
                sample["redis_used_cpu_sys"] = info.get("used_cpu_sys")
                sample["redis_used_cpu_user"] = info.get("used_cpu_user")
                sample["redis_connected_clients"] = info.get("connected_clients")
            except Exception:
                pass
        return sample

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self._sample_once())
            self._stop.wait(self.interval)

    def __enter__(self) -> "ResourceMonitor":
        if self._proc is not None:
            try:
                self._proc.cpu_percent(interval=None)  # prime the counter
            except Exception:
                pass
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        # Always take one final sample so short runs aren't empty.
        self.samples.append(self._sample_once())

    def summary(self) -> dict:
        if not self.samples:
            return {}
        keys: set[str] = set()
        for s in self.samples:
            keys.update(s.keys())
        keys.discard("t")

        out = {}
        for key in sorted(keys):
            vals = [s[key] for s in self.samples if s.get(key) is not None]
            if not vals:
                continue
            out[key] = {"min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals)}
        return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(v, default=str)
        else:
            out[key] = v
    return out


def write_result(result: dict, output: Optional[str], fmt: str = "json") -> None:
    """Print `result` to stdout, and additionally write it to `output` if given.
    `fmt` is 'json' or 'csv' (csv flattens nested keys with dotted paths)."""

    print(json.dumps(result, indent=2, default=str))

    if not output:
        return

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        flat = _flatten(result)
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(list(flat.keys()))
            writer.writerow(list(flat.values()))
    else:
        path.write_text(json.dumps(result, indent=2, default=str))

    print(f"\nWrote results to {path}", file=sys.stderr)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
