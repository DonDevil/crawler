#!/usr/bin/env python3
"""Deterministic priority + per-domain rate-limit scheduling probe.

Seeds a small, fixed workload across several domains with different
priorities, then calls `get_next_url()` repeatedly and reports the *actual*
claim order with timestamps -- so the interaction between global priority
(`domain_heads`/heap) and per-domain rate limiting (`domain_next_time`) can
be inspected directly, for either frontier backend.

Default scenario (`--scenario urgent:1:2,normal:5:2,bulk:10:1`, `--rate-limit
2.0`): domain "urgent" (priority 1, 2 URLs) becomes rate-gated immediately
after its first claim, so its second URL cannot be claimed again until
`rate_limit` seconds have passed -- during that window, lower-priority but
currently-eligible domains ("normal", then "bulk") get claimed instead, even
though "urgent" is globally the highest priority. This is the exact
rate-gated-domain-is-skipped-not-blocking behavior documented in
docs/architecture/frontier-adr.md §6.

`--scenario` format: comma-separated `domain:priority:count` triples.

Examples:
    python tests/benchmarks/priority_ratelimit.py
    python tests/benchmarks/priority_ratelimit.py --frontier redis --rate-limit 1.5
    python tests/benchmarks/priority_ratelimit.py \\
        --scenario fast:1:3,slow:1:3,filler:9:4 --rate-limit 1.0
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def parse_scenario(spec: str) -> list[tuple[str, int, int]]:
    triples = []
    for part in spec.split(","):
        domain, priority, count = part.split(":")
        triples.append((domain.strip(), int(priority), int(count)))
    return triples


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frontier", choices=["local", "redis"], default="local")
    p.add_argument("--scenario", default="urgent:1:2,normal:5:2,bulk:10:1",
                   help="Comma-separated domain:priority:count triples")
    p.add_argument("--poll-interval", type=float, default=0.05,
                   help="Sleep between get_next_url() polls while waiting out a rate gate")
    p.add_argument("--max-idle-polls", type=int, default=400)
    p.add_argument("--output", default=None)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    common.add_common_frontier_args(
        p,
        default_namespace="bench_priority",
        default_rate_limit=2.0,
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    domain_specs = parse_scenario(args.scenario)

    kwargs = common.frontier_kwargs_from_args(args)
    frontier = common.build_frontier(args.frontier, **kwargs)
    frontier.clear()

    run_id = f"pr{int(time.time())}"
    seeded = []
    for domain, priority, count in domain_specs:
        for i in range(count):
            url = f"https://{domain}-{run_id}.example.test/p{i}"
            frontier.add_url(url, priority=priority)
            seeded.append({"url": url, "domain": f"{domain}-{run_id}.example.test", "priority": priority})

    claim_order = []
    t_start = time.time()
    idle_polls = 0
    while True:
        claim = frontier.get_next_url()
        if claim is None:
            if not frontier.has_pending():
                break
            idle_polls += 1
            if idle_polls > args.max_idle_polls:
                break
            time.sleep(args.poll_interval)
            continue
        idle_polls = 0
        claim_order.append({
            "step": len(claim_order) + 1,
            "t_offset_s": round(time.time() - t_start, 3),
            "url": claim.url,
            "domain": claim.domain,
            "priority": claim.priority,
            "attempt": claim.attempt,
        })
        frontier.mark_visited(claim)

    final_status = frontier.get_status_counts()
    frontier.close()

    result = {
        "run": {"tool": "priority_ratelimit", "timestamp": common.now_iso(), "frontier": args.frontier},
        "config": {"scenario": args.scenario, "rate_limit_s": args.rate_limit},
        "seeded": seeded,
        "claim_order": claim_order,
        "final_status_counts": final_status,
        "total_elapsed_s": time.time() - t_start,
    }

    common.write_result(result, args.output, args.format)

    print("\nClaim order (domain, priority, offset):", file=sys.stderr)
    for entry in claim_order:
        print(f"  {entry['step']:>3}. t={entry['t_offset_s']:>6.3f}s  "
              f"domain={entry['domain']:<30} priority={entry['priority']:<3} "
              f"attempt={entry['attempt']}", file=sys.stderr)


if __name__ == "__main__":
    main()
