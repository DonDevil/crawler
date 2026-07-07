# Project Catch-Up

Snapshot date: 2026-07-04

## 1) What is already implemented

### Core runtime and CLI
- Main entrypoint is working with useful flags for mode and operations:
	- Query discovery controls (`--query`, `--query-only`, `--surface-web`, `--dark-web`)
	- Resume and cleanup (`--unfinished`, `--clear-db`)
	- Engine selection (`--crawler-engine auto|async|http|tor|playwright|selenium|scrapling`)
	- Crawl limits (`--max-pages`, `--indefinite-run`)
	- Media pipeline helper commands (`--claim-sample-job`, `--mark-match`)

### Config and persistence
- YAML config loading is implemented through typed models.
- URL crawl state is persisted in SQLite (`storage/crawl_state.db`) with statuses like queued, pending, visited, failed, skipped.
- Domain scoring storage exists and is used when a media match is confirmed.
- Separate media evidence SQLite storage is implemented (`storage/media_evidence.db`), including:
	- `media_assets`
	- `media_observations`
	- `sample_jobs`
	- `manifest_variants`

### URL frontier and crawl control
- Priority frontier is implemented with:
	- URL cleaning + dedupe
	- per-domain queueing
	- per-domain rate limiting
	- skip already visited URLs from DB
	- blacklist filtering at enqueue and dequeue time

### Discovery pipeline
- Query discovery system supports multiple engines with structured reports and dedupe:
	- DuckDuckGo, Bing, Brave, Yandex, Ahmia, Torch adapters exist
	- Surface-web and dark-web scope filtering is implemented
	- Discovery scoring/priority assignment is implemented
	- Onion URLs get priority boost
	- Blocked engines can be put on cooldown for later queries

### Crawlers (engines)
- Multiple crawler implementations are present and integrated:
	- Async crawler (aiohttp)
	- HTTP crawler (httpx)
	- Tor crawler (httpx + Tor proxy routing)
	- Playwright crawler
	- Selenium crawler
	- Scrapling crawler
- Hybrid auto-routing crawler is implemented:
	- Shared frontier
	- Per-URL engine plan
	- Escalation logic (lightweight fetch to browser-capable engines when needed)
	- Special handling for onion URLs via Tor

### Parsing and filtering
- HTML link extraction is implemented using BeautifulSoup + lxml.
- URL extraction from script/text is implemented.
- Media link detection is implemented (anchors, media tags, script/text discovery).
- Streaming manifest parsing is implemented for HLS/DASH variants.
- URL utility module is substantial (normalization, filtering, trap checks, relevance checks, blacklist checks).
- Link admission controls were improved to reduce queue explosion from low-value external links.

### Media evidence and future fingerprinter handoff
- Media evidence recording is integrated into crawler fetch/parse flow.
- Sampling jobs can be claimed.
- Matched assets can be marked and source-domain score can be increased.
- This provides a working handoff contract for a future fingerprinting service.

### Testing footprint
- Test suite is broad and covers major areas (frontier, crawlers, discovery, media evidence, search engines, CLI behavior).
- Existing analysis notes indicate large crawl-run improvements after queue-focus changes.


## 2) What we planned to do

Based on docs, code structure, and in-repo notes, the direction appears to be:

- Keep improving discovery quality while controlling queue growth.
- Use hybrid routing as default and escalate only when needed.
- Expand dark-web and anti-bot resilience.
- Build full media verification pipeline on top of current sample-job workflow.
- Integrate piracy detection/fingerprinting stack (image/video/audio).
- Move toward stronger domain intelligence and evidence generation.
- Improve operational reliability for long runs (nightly crawl quality, throughput, and stability).

Longer-term roadmap items mentioned in docs:
- Distributed crawler nodes
- AI-based piracy detection
- Automated evidence generation
- Larger-scale domain intelligence


## 3) What is remaining / gaps

### Partially implemented or still lightweight
- Several discovery helper modules are still minimal wrappers (domain expansion, torrent/darkweb seed utilities).
- Intelligence modules are basic (rule/list based) and not yet advanced scoring/ML.
- Some orchestration modules (scheduler/worker_pool/rate_limiter wrappers) exist but are not central to the current runtime path.

### Integration gaps
- Full end-to-end fingerprinting worker service is not yet implemented in this repo (queue/job DB contract exists, full worker pipeline does not).
- Export/reporting modules exist but are not a fully wired reporting workflow.
- Domain intelligence is present but still early-stage compared to planned goals.

### Operational/product gaps
- No distributed crawl execution layer yet.
- No Redis/PostgreSQL-backed runtime path currently used by the main loop (current primary persistence is SQLite).
- Browser and Tor paths can still be environment-dependent and need continuous hardening.
- CI/nightly automation and runbook-level observability can be improved.


## 4) Quick restart checklist

If restarting work now, practical next steps are:

1. Run current tests and note failures/regressions.
2. Confirm default `auto` crawl path behavior on a short run.
3. Prioritize one focused milestone:
	 - fingerprint worker implementation, or
	 - discovery quality improvements, or
	 - operational hardening for long runs.
4. Keep queue-growth metrics in each test crawl summary (attempted, success rate, completion rate, queue ratio).

