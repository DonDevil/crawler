# Nightly Crawl Run Analysis Log

Purpose: keep one running record of each nightly crawl so performance trends and improvements are easy to compare over time.

---

## Latest Optimization Status

**DATE: 2026-07-08 00:10 UTC**

**Optimizations Deployed:**

- ✅ Batched database writer (50x fewer commits)
- ✅ PRAGMA synchronous=NORMAL + busy_timeout=5000
- ✅ Query-aware link prioritization
- ✅ Rate limit: 0.3s/domain (was 1.0s)

**Baseline test needed:** Run `python main.py --indefinite-run` for 3+ hours to measure post-optimization performance.

---

## Summary Table

| Date and Time | Run Length | Found | Attempted | Visited | Failed | Queued | Success Rate | Completion Rate | Queue to Attempt Ratio | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-04-20 09:26:16 IST | 12h | 129,582 | 2,506 | 1,981 | 525 | 127,051 | 79.05% | 1.93% | 50.70x | Strong discovery, weak crawl completion, backlog growing too fast |
| 2026-04-20 22:12:07 IST | 11h | 42,538 | 20,495 | 17,830 | 2,665 | 19,189 | 87.01% | 48.20% | 0.94x | Significant improvement in completion rate and queue control |
| 2026-07-07 18:19:17 IST | 27h | 86,086 | 9,317 | 8,086 | 1,231 | 75,418 | 86.79% | 10.82% | 8.10x | **REGRESSION: Queue exploded, completion rate dropped 37%, discovery pressure quadrupled** |
| 2026-07-08 (PENDING) | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | **POST-OPTIMIZATION BASELINE** - Awaiting test run with batched DB writer & query-aware priority |

---

## Entry: 2026-04-20 09:26:16 IST

### Raw Run Result

- Total found websites: 129,582
- Total failed websites: 525
- Pirate domains inside failed set: 20
- Total visited websites: 1,981
- Total queued websites: 127,051

### Calculated Efficiency Metrics

- Total attempted websites = visited + failed = 2,506
- Successful visit rate = 1,981 / 2,506 = 79.05%
- Failure rate = 525 / 2,506 = 20.95%
- Crawl completion rate = 2,506 / 129,582 = 1.93%
- Remaining queue share = 127,051 / 129,582 = 98.05%
- Discovery pressure = 129,582 / 2,506 = 51.71 discovered sites for every processed site
- Successful visits per hour = 1,981 / 12 = 165.08
- Attempted sites per hour = 2,506 / 12 = 208.83
- Found sites per hour = 129,582 / 12 = 10,798.50
- Pirate-domain share of failures = 20 / 525 = 3.81%

### Analysis

This run is very good at finding websites, but not yet efficient at turning discoveries into completed crawls.

Main observations:

1. Discovery is massively outpacing crawl throughput.
   - The crawler is discovering about 51.7 sites for every site it actually processes.
   - That means the queue grows much faster than the workers can consume it.

2. The failure rate is noticeable but not the main bottleneck.
   - A 20.95% failure rate is worth improving, but the much bigger issue is uncontrolled queue growth.
   - Even with a better failure rate, the backlog would still be very large unless link admission stays focused.

3. The run is operationally productive, but not completion-efficient.
   - A 79.05% success rate among attempted sites is reasonable.
   - But only 1.93% of all found sites were actually attempted during the 12-hour window.

4. The queued count is the critical warning sign.
   - 127,051 queued sites means nearly all discovered sites are still waiting.
   - This indicates that filtering, prioritization, and crawl focus matter more than raw discovery volume.

5. The pirate-domain failure count is small in absolute terms.
   - Only 20 of 525 failures were pirate domains.
   - That suggests most failures are general crawl noise, protection, redirects, dead pages, or off-target domains.

### Overall Verdict

Current run efficiency:

- Discovery efficiency: High
- Crawl completion efficiency: Low
- Stability: Moderate
- Overall practical efficiency: Low to Moderate

---

## Entry: 2026-04-20 22:12:07 IST

### Raw Run Result

- Run length: ~11h
- Total found websites: 42,538
- Total failed websites: 2,665
- Total visited websites: 17,830
- Total queued websites: 19,189
- Total skipped websites: 2,829
- Total pending websites: 25
- Video links detected: 198

### Calculated Metrics

- Attempted = visited + failed = 17,830 + 2,665 = 20,495
- Success rate = 17,830 / 20,495 = 87.01%
- Failure rate = 2,665 / 20,495 = 12.99%
- Completion rate = 20,495 / 42,538 = 48.20%
- Queue share = 19,189 / 42,538 = 45.13%
- Discovery pressure = 42,538 / 20,495 = 2.08 discovered per attempted
- Visits per hour ≈ 17,830 / 11 ≈ 1,621
- Completion efficiency: 48.20% (BEST TO DATE)

### Analysis

Significant improvement in crawl completion efficiency. The queue is now much more controlled, and the success rate among attempted sites is high. Video link detection is working. Pending/skipped counts are small.

**This represents the best performance achieved so far.**

Key improvements from first run:

- Completion rate improved from 1.93% → 48.20% (25x better)
- Queue pressure reduced from 51.71x to 2.08x (25x better)
- Success rate improved from 79.05% → 87.01%
- Backlog reduced from 127k queued to 19k queued

---

## Entry: 2026-07-07 18:19:17 IST

### Raw Run Result

- Run length: 27.0 hours
- Total found websites: 86,086
- Total failed websites: 1,231
- Total visited websites: 8,086
- Total queued websites: 75,418
- Skipped: 1,325
- Pending: 26
- Media assets discovered: 493
  - Queued for fingerprinting: 165
  - Rejected (non-video): 293
  - Pending manual review: 17
  - Rejected (too short): 11
  - Uncertain (manual review): 4
  - Claimed matches: 3

### Calculated Metrics

- Attempted = visited + failed = 8,086 + 1,231 = 9,317
- Success rate = 8,086 / 9,317 = 86.79%
- Failure rate = 1,231 / 9,317 = 13.21%
- Completion rate = 9,317 / 86,086 = 10.82%
- Queue share = 75,418 / 86,086 = 87.67%
- Discovery pressure = 86,086 / 9,317 = 9.24 discovered per attempted
- Visits per hour = 8,086 / 27.0 ≈ 299.5
- Attempted sites per hour = 9,317 / 27.0 ≈ 345.1

### Fingerprinter Integration

⚠️ **NOTE: The fingerprinter tool was implemented, but the matching_status column was NOT found in the database.** Current media status shows initial categorization only (queued/rejected/pending review), not fingerprint matching results. This suggests:

- Fingerprinter may not be writing results to `media_assets` table
- OR fingerprinter is writing to a different table not yet integrated
- OR matching_status column schema needs to be added

**Action needed**: Verify fingerprinter output table/schema and update media_assets schema to include matching_status.

### Analysis & Comparison

#### ⚠️ MAJOR REGRESSION FROM APRIL 20 RESULTS

This run shows **significant degradation** in crawl efficiency compared to the peak April 20 performance:

**What Got Worse:**

1. **Completion Rate Collapsed**
   - April 20: 48.20%
   - July 07: 10.82%
   - **Regression: -37.38 percentage points (78% worse)**

2. **Queue Pressure Exploded**
   - April 20: 2.08x discovery pressure
   - July 07: 9.24x discovery pressure  
   - **Regression: 4.4x worse queue pressure**

3. **Queue Backlog Quadrupled**
   - April 20: 19,189 queued
   - July 07: 75,418 queued
   - **Regression: 3.9x more backlog**

4. **Throughput Reduced by 5.5x**
   - April 20: 1,621 visits/hour
   - July 07: 299.5 visits/hour
   - **Regression: -81% throughput loss**

5. **Absolute Visited Count Decreased**
   - April 20: 17,830 visited in 11h
   - July 07: 8,086 visited in 27h (more than double the time)
   - **Regression: 55% fewer visits despite 2.5x runtime**

#### What Stayed Stable

1. **Success Rate (among attempted)**
   - April 20: 87.01%
   - July 07: 86.79%
   - Minimal change, still good

2. **Failure Rate**
   - April 20: 12.99%
   - July 07: 13.21%
   - Minimal change

#### Critical Observations

1. **The queue control mechanism broke**
   - April 20 had intelligent filtering that kept queue at 19k despite finding 42k URLs
   - July 07 finds 86k URLs but can only process 9.3k (10.8% completion)
   - This suggests link filtering/prioritization has degraded or been disabled

2. **Crawl throughput bottleneck**
   - Visited/hour dropped from 1,621 → 299.5
   - This could indicate:
     - Rate limiting is too aggressive
     - Worker pool capacity reduced
     - Network issues or timeouts
     - Database write I/O bottleneck
     - Fingerprinter processing blocking the crawl

3. **Discovery is fine, crawl is broken**
   - Finding URLs works (86k found)
   - Failure rate is acceptable (13%)
   - But processing velocity is 5.5x slower
   - This is NOT a filtering problem, it's a throughput problem

### Overall Verdict

**Status: CRITICAL REGRESSION**

- Discovery efficiency: ✓ Good (still finding URLs)
- Crawl completion efficiency: ✗ **FAILED** (dropped from 48% to 11%)
- Throughput: ✗ **CRITICAL** (81% slower)
- Stability: ? Unknown (needs investigation)
- Overall practical efficiency: ✗ **VERY LOW** – worse than April 20 baseline

### Root Cause Hypotheses (Order of Likelihood)

1. **Fingerprinter integration is blocking crawl pipeline** - If fingerprinter operations are synchronously processing media before returning control to crawler, this would explain the 5.5x slowdown
2. **Database contention** - 493 media records + 1,406 observations being written while URLs table writes happening
3. **Rate limiting increased** - If rate-limit backoff was made more conservative
4. **Worker pool size reduced** - If async workers were reduced or disabled
5. **Network/environment change** - Different network conditions, ISP throttling, or target sites slower
6. **Indefinite mode changes** - Recent indefinite mode implementation may have altered queue strategy

### Required Actions

**URGENT:**

1. Profile crawl throughput vs media processing - determine if fingerprinter is blocking crawl
2. Check rate limiter settings - verify they haven't been made too conservative
3. Verify worker pool is sized correctly and all workers are active
4. Check database performance - verify no locking/contention issues
5. **Fix fingerprinter output schema** - ensure matching_status is being written to media_assets

**FOLLOW-UP:**
6. Implement adaptive rate limiting that maintains 1,000+ visits/hour target
7. Consider decoupling fingerprinter from main crawl pipeline (async processing)
8. Re-enable or re-balance link filtering that made April 20 so efficient

---

## Post-Optimization Baseline Test (2026-07-08)

### Optimizations Applied

**Database Layer:**

- Batched write operations (commit every 50 ops instead of 1)
- PRAGMA synchronous=NORMAL, busy_timeout=5000, cache_size=10000
- Thread-safe connection parameters
- ~50x reduction in disk sync operations

**Link Prioritization:**

- Query-aware link boosting (+4 priority for query-matching URLs)
- Query context flows from discovery → frontier → link extraction
- Backward compatible with non-query link extraction

**Configuration:**

- Rate limit: 0.3s/domain (improved from 1.0s)

### How to Run Baseline Test

```bash
cd /home/darkdevil/Desktop/anti_piracy/crawler
source env/bin/activate

# Start crawl - let it run for 3+ hours
python main.py --indefinite-run
# Watch logs, note start time and initial database state

# After crawl completes or reaches steady state, check results:
python3 << 'EOF'
import sqlite3
from datetime import datetime

conn = sqlite3.connect('storage/crawl_state.db')
cursor = conn.cursor()

print("=== POST-OPTIMIZATION METRICS ===\n")
cursor.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")
print("URL Status Distribution:")
stats = {}
for status, count in sorted(cursor.fetchall()):
    stats[status] = count
    print(f"  {status:15}: {count:7,}")

total = sum(stats.values())
visited = stats.get('visited', 0)
failed = stats.get('failed', 0)
queued = stats.get('queued', 0)
attempted = visited + failed

if attempted > 0:
    success_rate = (visited / attempted) * 100
    completion_rate = (attempted / total) * 100
    discovery_pressure = total / attempted
    print(f"\n  TOTAL           : {total:7,}")
    
    print(f"\nEfficiency Metrics:")
    print(f"  Success Rate: {success_rate:.2f}%")
    print(f"  Failure Rate: {100-success_rate:.2f}%")
    print(f"  Completion Rate: {completion_rate:.2f}%")
    print(f"  Discovery Pressure: {discovery_pressure:.2f}x")
    print(f"  Queue Backlog: {queued:,}")

EOF
```

### Baseline Results Template

Once test completes, fill in this template and add to summary table above:

```
Date: 2026-07-08 HH:MM:SS UTC
Run Length: ??h
Found: ??
Attempted: ??
Visited: ??
Failed: ??
Queued: ??
Success Rate: ??%
Completion Rate: ??%
Discovery Pressure: ??x

Key Observations:
- Visited/hour: ?? (target: 1,000+)
- Improvement vs regression run: ??%
- Query boosting observed: yes/no
- Database write performance: (smooth/contention/blocking)
- Any errors or issues: (list)
```

### Comparison Targets

**Expected Improvements (vs 2026-07-07 regression):**

- Visited/hour: ≥1,000 (from 299.5) = 3.3x improvement
- Completion rate: ≥30% (from 10.82%) = 2.8x improvement
- Queue pressure: ≤5x (from 9.24x) = 1.8x improvement
- Success rate: ≥85% (maintain ~87%)

**Recovery Target (vs April 20 peak):**

- Visited/hour: 1,200+ (original 1,621)
- Completion rate: 30%+ (original 48% - may be lower due to larger initial queue)
- Queue pressure: <3x (original 2.08x)

## Redis Overnight Run — 2026-08-10/11

### Configuration

- Backend: Redis
- Workers: 25 async workers
- Runtime: 5h 9m 36s
- Rate limit: 0.3s/domain
- Search engines: DuckDuckGo, Bing, Brave, Yandex, Ahmia, Torch
- Crawl mode: seeds + query
- Query: BLAST full movie download
- Indefinite run: yes
- Media evidence: enabled

### Raw Results

- Discovered: 38,550
- Visited: 14,868
- Queued at end: 0
- Inflight at end: 1
- Reported failed_permanent: 38,503

### Reliable Throughput

- Visited/sec: 0.800
- Visited/hour: ~2,881

### Resource Usage

- Process CPU average: 5.54%
- Process CPU peak: 57.9%
- Process RSS average: 433.8 MB
- Process RSS peak: 512.1 MB

### Redis Usage

- Memory start: 31.7 MB
- Memory end: 43.2 MB
- Sampled memory peak: 61.7 MB
- Connected clients average: 5.95
- Connected clients peak: 6
- Redis CPU time: 51.63 seconds total

### Comparison

- ~1.78x April 20 throughput
- ~9.62x July 7 throughput
- Queue drained to effectively zero
- Redis CPU utilization is negligible
- Crawler process is not CPU-bound

### Important Reporting Issue

`visited + failed_permanent = 53,371`, exceeding
`discovered_total = 38,550`.

Therefore `failed_permanent` cannot currently be treated as a
unique-URL terminal count, and failure rate/completion rate derived
from these counters are invalid for benchmark comparison.

### Verdict

The Redis-backed crawler demonstrates substantially improved
throughput and queue control compared with the July regression.

The current bottleneck does not appear to be Redis CPU, Redis memory,
or crawler CPU. The workload is likely predominantly network/remote
site latency bound.

Before declaring this the final performance baseline, fix the
counter semantics and repeat the benchmark under controlled worker
counts.