# Nightly Crawl Run Analysis Log

Purpose: keep one running record of each nightly crawl so performance trends and improvements are easy to compare over time.

---

## Summary Table

| Date and Time | Run Length | Found | Attempted | Visited | Failed | Queued | Success Rate | Completion Rate | Queue to Attempt Ratio | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-04-20 09:26:16 IST | 12h | 129,582 | 2,506 | 1,981 | 525 | 127,051 | 79.05% | 1.93% | 50.70x | Strong discovery, weak crawl completion, backlog growing too fast |

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

In short: the crawler is excellent at expanding the frontier, but before the recent focus improvements it was not selective enough, so the queue exploded faster than the system could process it.

### Baseline for Future Nightly Comparisons

Use this entry as the baseline. On future nights, compare these values first:

- Attempted sites per hour
- Success rate
- Completion rate
- Queue to attempt ratio
- Total queued backlog
- Pirate-domain failure count

Target direction for improvement:

- Higher visited or attempted per hour
- Lower queue growth
- Lower failure rate
- Better completion rate from found to processed
- More relevant domains and less off-target discovery noise

---

## Template for the Next Nightly Entry

Copy this block and append a new section below each night:

### Entry: YYYY-MM-DD HH:MM:SS TZ

- Run length: 12h
- Total found websites:
- Total failed websites:
- Pirate domains inside failed set:
- Total visited websites:
- Total queued websites:

Metrics to compute:

- Attempted = visited + failed
- Success rate = visited / attempted
- Failure rate = failed / attempted
- Completion rate = attempted / found
- Queue share = queued / found
- Discovery pressure = found / attempted
- Visits per hour = visited / 12

Short note:

- What improved:
- What got worse:
- Main bottleneck now:
