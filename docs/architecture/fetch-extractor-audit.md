# Fetch / Extractor Pipeline Audit

Status: Audit only
Implementation changes: None
Date: 2026-08-11
Source: Fetch/extractor audit completed by Claude Code

> This document is the archived record of a read-only investigation into
> the crawler's search-discovery, Tor, Selenium, Playwright, and HTTP
> fetch/extract pipeline, performed ahead of the first overnight
> real-world crawl. It was originally delivered as an interactive report;
> this file is the durable, in-repo copy of the same findings, unabridged.
> No source, configuration, dependency, or test file was changed to
> produce it (confirmed via `git status`/`git diff --stat` before and
> after the investigation — see §17 and §18).

## Scope & method

The prior optimization work on this crawler concentrated on the Redis
frontier, the SQLite/Redis boundary, claims/leases, recovery, scheduling,
domain starvation, Media Evidence, and benchmarking/reporting. It did
**not** deeply audit whether the actual discovery/fetch/extraction
pipeline — search engines, Tor, Selenium, Playwright — is functioning
correctly. This audit closes that gap.

Method: source reading of the current tree (not archived docs), plus
one-shot, read-only live diagnostics run directly through the crawler's
own implementation classes (real search-engine HTTP requests, a real
`.onion` fetch over the local Tor daemon, a real Playwright browser
launch, a real Selenium driver launch) — each torn down cleanly
afterward. Every conclusion below is labeled as either **live-verified**
(an actual diagnostic was run and produced the cited output) or
**code-only** (static reading; no live check was performed or possible).
Nothing here is guessed; where evidence was insufficient to reach a
verdict, the finding is marked **UNVERIFIED**/**UNKNOWN** rather than
assumed.

---

## 1. Current fetch/extractor architecture

Entry point `main.py` → `CrawlerManager` (`core/crawler_manager.py`)
reads `crawler.engine` (config.yaml default: **`auto`**) and constructs
**`HybridCrawler`** (`crawler/hybrid_crawler.py`) — this is the real,
default, production execution path. Six standalone single-engine classes
also exist (`AsyncCrawler`, `HTTPCrawler`, `TorCrawler`,
`PlaywrightCrawler`, `SeleniumCrawler`, `ScraplingCrawler`) and are only
used if `--crawler-engine <name>` is passed explicitly — in standalone
mode, that one engine fetches **every** URL with no escalation.

```
main.py
  → CrawlerManager.run()
      → prepare_frontier()  [sync, runs BEFORE any asyncio worker task exists]
          → load_seeds()  (seeds/piracy_sites.txt, 51 URLs / ~50 domains)
          → load_search_query_urls() → discovery/search_engine_discovery.py
              → 6 search engines queried serially, synchronously (httpx.Client)
              → score_discovered_url() → frontier.add_url()
      → HybridCrawler.run()
          → scheduler():  frontier.get_next_url() (claim) → asyncio.Queue
          → worker() × concurrency (25 by default), each:
              claim = queue.get()
              → CrawlerRouter.get_engine_plan(url)
                    .onion  → ["tor"]                                    (no fallback)
                    other   → ["async", (scrapling), "playwright", "selenium", "http"]
              → _run_engine_plan(): try engines in order, escalate on
                failure OR CrawlerRouter.needs_browser_upgrade(html)
              → on success: HTMLLinkExtractor / MediaLinkDetector /
                StreamingManifestParser (parsers/*.py) extract links + media
              → media_database.record_media_link(...)   [SYNC, see §8]
              → frontier.add_url(link) for each discovered link
              → frontier.mark_visited(claim) / mark_failed(claim, reason)
```

Key classes/files: `core/crawler_manager.py` (orchestrator),
`core/crawler_router.py::CrawlerRouter` (routing heuristics — the real
decision logic), `crawler/hybrid_crawler.py::HybridCrawler` (the actual
running engine), `core/claim_heartbeat.py`, `core/frontier_executor.py`
(`AsyncFrontier`), `parsers/*.py` (shared extraction layer, identical for
every fetch engine, run inline/synchronously after each fetch — see §8).

Every claim's whole engine-escalation chain (potentially several engine
attempts) is wrapped by `run_with_heartbeat` (`core/claim_heartbeat.py`),
which renews the frontier claim on its own timer, independent of how long
the underlying fetch takes — this decouples claim ownership from fetch
duration (relevant to the Playwright timeout finding in §9).

**Confidence: live-verified for the default engine selection**
(`config.yaml`'s `crawler.engine: "auto"` → `core/crawler_manager.py:186,211`
read directly) and **code-verified** for the rest of the call graph.

---

## 2. Search-engine status (every engine)

Live-tested on 2026-08-11 against each engine's real `search()` method,
through the crawler's own implementation, from this environment.

| Engine | Status | Evidence |
|---|---|---|
| DuckDuckGo | **WORKING** | Live: real results returned, redirect-unwrap correct |
| Bing | **WORKING** | Live: 5 correctly decoded destination URLs; `site:` operator queries return 0 results (Bing UX layout quirk, not a parser bug) |
| Brave | **ENVIRONMENT-DEPENDENT** | Live: single HTTP 429 from this sandbox's IP — correctly raised/logged, not proven broken; needs repeated spaced sampling to classify further (see §18) |
| Yandex | **BROKEN (externally)** | Live: redirected to `yandex.com/showcaptcha`, a real anti-bot wall — see §3 |
| Ahmia | **WORKING** | Live: 5 real `.onion` results |
| Torch | **WORKING** | Live: 5 real `.onion` results (this result also independently confirms Tor SOCKS connectivity works in this environment) |

### Common architecture (all six engines)

- Contract: `search_engines/base.py::BaseSearchEngine` (abstract `search()`).
- HTTP mechanism: **synchronous** `httpx.Client` (`base.py:53`), one
  request per call, `follow_redirects=True`, `Accept-Encoding: identity`
  forced (`base.py:42`).
- Timeout: `search.timeout` (15s, `config.yaml:99`), no connect/read
  split, **no HTTP-layer retry** — one shot per `search()` call.
- Errors: `httpx.HTTPStatusError`/`HTTPError` wrapped and re-raised as
  `SearchEngineUnavailableError` (`base.py:62-67`) — never silently
  swallowed at this layer.
- Orchestration: `discovery/search_engine_discovery.py`.
  `build_search_engines()` instantiates one client per configured engine
  via `ENGINE_REGISTRY` (`:20-27`).
  `discover_urls_from_query_with_report()` (`:134`) calls each engine's
  `.search()` **synchronously and serially**; `SearchEngineBlockedError`/
  `SearchEngineError` are logged and recorded in `report.engine_errors`,
  then discovery continues to the next engine (`:185-190`); unexpected
  exceptions are also caught and logged via `logger.exception`
  (`:191-193`) — no engine failure can crash discovery, and every failure
  is visible in the returned report, not swallowed into an empty list.
- This whole discovery call chain runs from
  `core/crawler_manager.py::load_search_query_urls` →
  `prepare_frontier()`, called synchronously at the very start of
  `CrawlerManager.run()` (`:516`), **before any asyncio worker tasks
  exist** — deliberate per the file's own comment (`:177-180`): the sync
  HTTP calls here don't block concurrent event-loop work because there
  isn't any yet. Net effect: for N queries × 6 engines × up to 15s
  timeouts, a worst case (several engines timing out) is minutes of dead
  time before the first page fetch — real, measurable startup latency
  worth capturing on the overnight run (see §16).
- Dead code confirmed: `search_engines/custom_query_generator.py` has
  **zero importers anywhere in the tree** — matches
  `docs/architecture/system-architecture.md` §25.

### Scheduling quirk (affects Yandex specifically, §3)

`discovery/search_engine_discovery.py:256`:
```python
if "captcha" in error.lower() or "blocked" in error.lower() or engine_name == "yandex":
    blocked_engines[engine_name] = max(blocked_engine_cooldown_queries, 1)
```
Yandex is **hardcoded by name** into the cooldown-blocklist trigger — any
failure of any kind (not just a captcha/blocked error) has the same
effect as a confirmed captcha block. Combined with
`blocked_engine_cooldown_queries: 999` (`config.yaml:108`), one Yandex
failure on the first query effectively disables Yandex for the rest of
any realistic run. Structurally deliberate (Yandex is singled out by
name), but it means the logs cannot distinguish "Yandex timed out once"
from "Yandex is captcha-walled" — both look identical.

### Per-engine detail

**DuckDuckGo** (`search_engines/duckduckgo_search.py`) — endpoint
`https://html.duckduckgo.com/html/` (the no-JS HTML endpoint, correct
choice for scraping). Selector `a.result__a[href]`; redirect unwrapping
via `clean_result_url` decodes DDG's `/l/?uddg=` wrapper (`:17-23`). Live:
`engine.search("test query", max_results=5)` returned real results (e.g.
`http://www.example.com/`). No captcha/anti-bot handling coded (none
observed either). No DuckDuckGo-specific test exists.

**Bing** (`search_engines/bing_search.py`) — endpoint
`https://www.bing.com/search`. Selectors `li.b_algo h2 a[href]`,
`li.b_algo a[href]` (fallback). Redirect unwrapping decodes Bing's base64
`/ck/a?u=a1...` tracking wrapper (`:18-34`), including stripping a
leading `a1` prefix and fixing base64 padding — non-trivial, and verified
live against a real captured `href`. Live: `"python programming"` → 5
correct, fully-decoded URLs (python.org, w3schools, etc.); 10 anchors
matched under the live selector. Caveat: a `site:example.com` query
returned 0 results — Bing renders a different layout for `site:` operator
queries in this sample; flagged as a known edge case, not evidence the
general parser is broken. `tests/search_engine_test.py` tests only
`clean_result_url`'s base64-decode path (one hardcoded string), not live
`search()`/parsing.

**Brave** (`search_engines/brave_search.py`) — endpoint
`https://search.brave.com/search`. Selectors `div.heading a[href]`,
`div.snippet a[href]`, `h2 a[href]`. No redirect-unwrapping needed. Live:
`SearchEngineUnavailableError: HTTP 429 from brave`, observed once, from
this sandbox's IP, on the first request — correctly raised as a typed
exception (`base.py:62-65`) and correctly recorded in
`report.engine_errors`, not silently swallowed. Whether this is genuine
sustained rate-limiting of this environment's IP or a one-off is
**UNKNOWN without repeated, spaced-out sampling**, which was not
performed here to avoid hammering Brave's servers (see §18). No retry
logic exists anywhere in the stack to recover from a single 429 within
one `search()` call.

**Yandex** (`search_engines/yandex_search.py`) — see dedicated §3.

**Ahmia** (`search_engines/ahmia_search.py`) — dark-web engine, reachable
over clearnet (Ahmia itself is a surface-web index of onion sites, no Tor
proxy needed for this engine specifically). Two-step: GET `ahmia.fi/` to
find the search `<form>` and its hidden CSRF-style token inputs
(`:26-36`), then GET `ahmia.fi/search/` with those params. Selectors
`a[href*='redirect_url=']`, `a[href*='.onion']`; `clean_result_url`
unwraps the `redirect_url=` param. Live: returned 5 real `.onion` URLs for
query `"market"`. Fragile point (static observation, not currently
failing): if `home_soup.find("form", action="/search/")` returns `None`,
raises `SearchEngineParsingError("Ahmia search form not found")`
(`:28-29`) — correctly typed, would surface visibly if Ahmia ever changes
its homepage form structure.

**Torch** (`search_engines/torch_search.py`) — dark-web engine, requires a
working Tor SOCKS proxy (`tor.proxy_config.get_default_tor_proxy()`),
fetched via the same synchronous `httpx.Client(proxy=...)` path as every
other engine. Tries three hardcoded `.onion` mirror URLs in sequence
(`BASE_URLS`, `:16-20`), first non-empty-result mirror wins; collects
per-mirror errors and only raises a combined
`SearchEngineUnavailableError` if *all three* fail (`:38-60`) — reasonable
resilience design. Selector is the broadest of any engine (`a[href]`,
`:50`), relying entirely on `clean_result_url` to filter nav/index/
advertise/search pages rather than a precise results selector — more
fragile to markup drift than the others, but currently working. Live:
`engine.search("market", max_results=5)` returned 5 real `.onion` URLs
through the first mirror.

---

## 3. Yandex diagnosis

**Root cause: EXTERNAL BLOCKING (anti-bot captcha) — not a code bug, not
a parser problem, not a timeout, not a proxy issue, not disabled config,
not an exception-handling defect.**

Live diagnostic: `YandexSearch().search("test query")` raised:
```
SearchEngineBlockedError: Yandex requires captcha verification for this
IP/session; HTML scraping is blocked upstream
```
Traced deeper by replicating Yandex's own request exactly
(`base.py::_fetch_html` against `yandex_search.py::BASE_URL` +
`params={"text": ...}`): Yandex's server redirects (via
`follow_redirects`) to:
```
https://yandex.com/showcaptcha?cc=1&form-fb-hint=1.1&mt=...&retpath=...
```
with page `<title>Are you not a robot?</title>` — Yandex's own
anti-automation interstitial, confirmed both by `showcaptcha` in the
final URL and the page title, exactly what `yandex_search.py:17` checks
for (`"showcaptcha" in final_url.lower() or ... "verification" in
soup.title...`).

**The detection code is correct and working as designed** — it
recognizes the block and raises a typed `SearchEngineBlockedError` rather
than misinterpreting the captcha page as "zero results" or crashing on an
unexpected page structure. No evidence of a parser bug, stale selector,
timeout, proxy misconfiguration, or exception-handling defect. Root cause
is squarely Yandex's own anti-bot system rejecting unauthenticated/
automated HTTP scraping from this IP — the same outcome any simple
`httpx`/`requests`-based scraper without a real browser, residential IP,
or session cookies would hit against Yandex today. This is an
external-service reality, not something fixable by touching
`yandex_search.py`. A real fix (out of scope for this audit) would need a
different acquisition strategy entirely — a real browser session with
persistent cookies, different IP reputation, or dropping Yandex — not a
parser fix.

One compounding *code-level* issue (not the cause of the block, but it
affects how the crawler reacts to it): the hardcoded
`engine_name == "yandex"` cooldown rule (§2) means the very first Yandex
captcha hit disables Yandex for the rest of the run — appropriate given
the block is real and persistent per IP, but it also means a future
transient-only failure would look identical in logs to a full captcha
block.

**Confidence: live-verified.** This is not a hypothesis.

---

## 4. Tor / `.onion` diagnosis

**Status: WORKING, environment-dependent on a live Tor daemon.**

### Dead code confirmation

`docs/architecture/system-architecture.md` §25 claims `tor/tor_manager.py`,
`tor/onion_router.py`, and `discovery/darkweb_discovery.py` are dead code.
**Confirmed by grep**: no file in the tree imports `TorManager`,
`OnionRouter`, or anything from `discovery.darkweb_discovery` /
`load_onion_seeds` outside their own definitions.

- `tor/tor_manager.py`: `TorManager` would `subprocess.Popen(["tor",
  "--DataDirectory", ...])` to launch a Tor process itself — never
  called. The crawler does **not** start its own Tor process; it only
  ever *detects* an already-running one.
- `tor/onion_router.py`: `OnionRouter` is a thin unused wrapper around
  `tor.proxy_config.get_default_tor_proxy()` — functionally redundant
  with code that calls `get_default_tor_proxy()` directly (which
  everything actually uses).
- `discovery/darkweb_discovery.py`: `load_onion_seeds()` (filter a seed
  file to `.onion` URLs) — never called; seed loading goes through
  `discovery/piracy_site_seeds.py::load_seeds` directly, no onion
  filtering step.

### The real proxy path — `tor/proxy_config.py`

`get_default_tor_proxy()` (`:30-41`):
1. `TOR_SOCKS_PROXY` env var, if set, wins outright (full proxy URL).
2. Else `TOR_SOCKS_PORT` env var (port only, host fixed at `127.0.0.1`).
3. Else probes `127.0.0.1:9050` then `127.0.0.1:9150` with a raw TCP
   `connect_ex` (0.5s timeout, `:13-16`) and uses whichever is open.
4. Else **falls back to `socks5h://127.0.0.1:9050` unconditionally**,
   even if nothing is listening — no explicit "Tor unavailable" signal
   at this layer; failure only surfaces later, at actual connection time,
   as a generic httpx/connection exception.

**Live state of this sandbox**: a real system Tor daemon is running
(`/usr/bin/tor --defaults-torrc ... -f /etc/tor/torrc`, confirmed via
`ps aux`) and listening on port 9050 (confirmed via a raw socket probe:
`_is_local_port_open('127.0.0.1', 9050)` → `True`). This is a
pre-existing OS-level Tor service, not something the crawler started.
`get_default_tor_proxy()` correctly resolves to `socks5h://127.0.0.1:9050`.

### Actual page-fetch path — `crawler/tor_crawler.py`

`TorCrawler` (used both as the standalone `--crawler-engine tor` engine,
`core/crawler_manager.py:218`, and as `HybridCrawler`'s `_tor_engine` for
onion URLs, `crawler/hybrid_crawler.py:113`) is **fully async** — two
`httpx.AsyncClient` instances (`run()`, `:264-275`): one built with
`proxy=get_default_tor_proxy()` (`tor_client`), one with no proxy
(`direct_client`). `fetch()` (`:63-125`) is a plain `async def` doing
`await client.get(url)` — no blocking calls, no `asyncio.to_thread`
needed. **Async-safe.**

Per-URL routing inside `fetch()` (`:71-72`):
```python
use_tor = URLUtils.is_onion_url(url) or self.use_tor_for_clearweb
client = tor_client if use_tor else direct_client
```
`use_tor_for_clearweb` defaults to `False` and is **never set from
config.yaml or core/config.py anywhere in the codebase** (grepped both,
zero references) — always `False` in practice. This means standalone
`--crawler-engine tor` does **not** route every URL through Tor: it
fetches `.onion` URLs via the SOCKS proxy and every other URL directly
over clearnet in the same run, silently. If the intent of
`--crawler-engine tor` was "everything through Tor," that is not what the
code does today.

`is_onion_url` (`utils/url_utils.py:601-607`): simple, correct hostname
suffix check (`parsed.hostname.endswith(".onion")`), wrapped in a broad
`except Exception: return False` — malformed URLs are treated as
non-onion rather than raising (reasonable defensive behavior).

Timeout: `timeout=20` default constructor arg (`tor_crawler.py:30`) — 5s
higher than the shared 15s default elsewhere, but both real construction
sites (`core/crawler_manager.py`, `hybrid_crawler.py`) pass `timeout`
explicitly (the shared `crawler.timeout` value), so this 20s default is
currently dead weight, not actually used — a minor inconsistency, not a
bug.

Retry: up to `max_retries` (default 3) with `asyncio.sleep(1)` between
attempts, but only on 5xx status codes or exceptions (`:82-84`,
`:120-123`) — 4xx is not retried (`:86`), which is correct behavior.

Error handling: every failure path returns `(None, error_string)` up to
`worker()`, mapped into `frontier.mark_failed(claim, failure_reason)`
(`:183-188`) — logged via `logger.warning`/`.error` at multiple points
(`:80`, `:122`, `:181`, `:212-226`) and recorded against the specific
URL's claim, not silently dropped. `FrontierUnavailable` and
`ClaimLostError` are caught distinctly, consistent with the rest of the
codebase's frontier-failure-semantics pattern.

### Routing decision — does the default (`auto`) path actually attempt onion fetches?

Yes. `core/crawler_router.py`:
- `select_crawler(url)` (`:77-83`): returns `"tor"` if
  `URLUtils.is_onion_url(url)`, else `"async"`.
- `get_engine_plan(url, ...)` (`:129-174`): first line (`:138-139`) is
  `if URLUtils.is_onion_url(url): return ["tor"]` — unconditionally,
  regardless of `current_engine`/`failure_reason`/`html`.
- `needs_browser_upgrade(url, ...)` returns `False` immediately for onion
  URLs (`:93-94`) — browser escalation is explicitly disabled for
  `.onion` targets.

**Consequence — no fallback for onion URLs**: in
`HybridCrawler._run_engine_plan` (`:218-274`), after a `"tor"` attempt
fails, re-calling `get_engine_plan(url, current_engine="tor", ...)`
re-enters the `is_onion_url` branch and returns `["tor"]` again;
`_prepend_unique` filters it out since it's already in `attempted`, so
the merged plan is **empty** and the loop simply ends. A failed onion
fetch has **zero engine-level escalation path** (though `TorCrawler.fetch()`
itself still retries up to 3× internally before reporting failure) —
architecturally consistent (no other engine here can fetch over Tor), but
means a transient Tor circuit failure gets exactly 3 quick retries (1s
apart) and then relies entirely on the frontier's own backoff to try
again later.

### Live diagnostic result

Reused one of `search_engines/torch_search.py`'s own `BASE_URLS` mirror
addresses and replicated `TorCrawler.fetch()`'s exact client construction
(`httpx.AsyncClient(timeout=20, follow_redirects=True,
proxy=get_default_tor_proxy())`):
```
proxy: socks5h://127.0.0.1:9050
status: 200  len: 7693  onion? True
```
A real `.onion` page fetch over the real Tor SOCKS proxy, using the exact
code path `TorCrawler.fetch()` uses, **succeeded** in this environment.

### Test coverage

`tests/tor_test.py` only tests `proxy_config.get_default_tor_proxy()`'s
port-selection logic (mocked `_is_local_port_open`) and
`TorchSearch.search()`'s URL-parsing logic (mocked `_make_soup`, no real
network). **No test anywhere in the repo exercises `TorCrawler.fetch()`
or a real/simulated onion fetch** — the only verification that
`TorCrawler` actually works is this audit's live diagnostic, not the test
suite.

**Confidence: live-verified**, with the explicit caveat that the "no Tor
running" failure mode itself was not observed live (no signal exists in
the code for it — see §18).

---

## 5. Selenium diagnosis

**Status: WORKING, live-verified.**

### Async safety — correct

`fetch()` (`crawler/selenium_crawler.py:130-142`):
```python
async def fetch(self, url: str) -> tuple[Optional[str], Optional[str]]:
    for attempt in range(1, self.max_retries + 1):
        html, error = await asyncio.to_thread(self._fetch_sync, url)
        ...
```
The synchronous, blocking Selenium calls (`_fetch_sync`, `:109-128`:
`_make_driver()`, `driver.get(url)`, `driver.page_source`,
`driver.quit()`) are entirely wrapped in `asyncio.to_thread`, matching
the same pattern used for `HybridCrawler`'s Selenium readiness probe
(`_ensure_selenium_ready`, `hybrid_crawler.py:145-165`, also
`to_thread`-wrapped). **Selenium usage does not stall other concurrent
async fetches (aiohttp/httpx/Playwright) in `HybridCrawler`.**

Residual caveat: `asyncio.to_thread` cannot forcibly interrupt a running
synchronous thread — if the enclosing task is cancelled while
`_fetch_sync` is mid-flight, the thread keeps running until Selenium's
own `set_page_load_timeout` fires or `driver.get()` returns naturally.
`driver.quit()` still runs (`try/finally`, `:123-128`) once the thread
function returns, so this does not leak the browser process, but a fast
shutdown could wait up to `self.timeout` for one in-flight fetch's thread
to finish.

### Driver lifecycle — new Chrome process per fetch, not reused

`_fetch_sync` (`:109-128`) calls `self._make_driver()` and `driver.quit()`
on **every single fetch** — no driver pooling or reuse. **Live-measured**:
`_make_driver()` → ready driver in `0.47s`, then
`driver.get('https://example.com')` → `1.90s`, total `2.56s` for one
fetch including full process teardown. For a JS-heavy real-world piracy
page this would be meaningfully higher. This full-relaunch-per-fetch
design is the main resource-cost concern for Selenium: N fetches ≈ N full
browser process lifecycles, unlike Playwright's one shared browser
serving N fetches (§6).

`_make_driver()` (`:66-107`) builds Chrome with a hardened flag set
(`--headless=new`, `--no-sandbox`, `--disable-dev-shm-usage`,
`--disable-gpu`, image-loading disabled via Chrome prefs, a **fixed**
`--remote-debugging-port=9222` — a theoretical collision risk for two
concurrent driver instances on the same machine, not independently
verified beyond the single-instance test here). Chrome binary
auto-detected via `shutil.which("google-chrome"/"chromium"/
"chromium-browser")` (`:71-75`), falling back to Selenium 4's built-in
Selenium Manager if none found — confirmed installed version **4.41.0**,
`requirements.txt:24` pins `selenium` with no explicit driver-manager
package.

### Concurrency

- `SeleniumCrawler.__init__` self-caps at **4** regardless of the
  `concurrency` argument (`min(concurrency, 4)`, `:46`) — even standalone
  `--crawler-engine selenium` (which would otherwise get
  `crawler.concurrency`=25) is hard-limited to at most 4 concurrent
  Chrome processes.
- Inside `HybridCrawler`, further restricted to
  `self._selenium_semaphore = asyncio.Semaphore(1)`
  (`hybrid_crawler.py:93`) — **at most one Selenium fetch at a time**
  across the whole hybrid run.

### Timeout

`self.timeout` (class default `30`, `:36`) applied via
`driver.set_page_load_timeout(self.timeout)` (`:106`) — a genuine
browser-level page-load timeout, not just an HTTP timeout. When
constructed via `HybridCrawler`/`CrawlerManager`'s shared `common_args`,
`timeout` is passed explicitly as `crawler.timeout` (15 by default),
overriding this class's own `30` default — so in practice Selenium runs
with the same 15s timeout as every other engine, not its own more
generous 30s. No Selenium-specific timeout knob exists in
`config.yaml`/`core/config.py` (confirmed via grep).

### Retry

`max_retries` class default `2` (`:37`; overridden to the shared value,
typically 3, via Hybrid/Manager). Each retry (`fetch()`, `:133-140`)
calls `_fetch_sync` again from scratch — since a new driver is created
every call anyway, a retry after a `WebDriverException` (crashed/hung
browser) gets a genuinely fresh browser process. 1s `asyncio.sleep`
between attempts (`:140`).

### Exception handling / error visibility

`_fetch_sync` (`:109-128`) catches `WebDriverException` specifically
and falls back to a broad `except Exception` — both return
`(None, error_string)`, never silently return an empty success. Logged at
`logger.warning` in `fetch()` (`:139`) and again in `worker()`
(`:198`, `:229-243`); the failure reason reaches
`frontier.mark_failed(claim, failure_reason)` (`:201`).

**`driver.quit()` itself is wrapped in `try/except: pass`
(`:124-128`) — a failure to quit is deliberately swallowed with
zero logging.** This is the single fully-silent exception handler found
anywhere in the entire fetch/extractor codebase (see §10, §18): a hang or
repeated failure in `quit()` would leak Chrome processes with no signal
in the crawler's own logs, only visible at the OS process-accounting
level.

### Failure classification (from code + live test)

- **Initialization**: handled — `webdriver is None` (import failed)
  raises `RuntimeError("selenium is not installed")` explicitly
  (`:67-68`); not observed live (imports fine, driver creates in 0.47s).
- **Driver-binary/browser mismatch**: not observed live; would surface as
  `WebDriverException` from `webdriver.Chrome()`, caught by the broad
  `except Exception` in `_fetch_sync`.
- **Browser crash/page timeout**: handled via `WebDriverException` +
  `set_page_load_timeout`; not observed live.
- **Selector/extraction failure**: N/A — `SeleniumCrawler` returns raw
  `driver.page_source` only; all extraction happens downstream in the
  shared parser layer, identical for every engine.
- **Resource exhaustion**: bounded by the concurrency=4 self-cap
  (standalone) / semaphore=1 (Hybrid); not stress-tested.
- **Concurrency problem**: none found — `asyncio.to_thread` usage correct
  throughout.
- **Proxy/Tor problem**: Selenium has **no Tor/proxy integration at all**
  — `_make_driver()`'s Chrome options include no `--proxy-server` flag,
  no reference to `tor.proxy_config`. Selenium cannot fetch `.onion` URLs
  in this codebase (consistent with the router never routing onion
  traffic to it).

### Live diagnostic

Direct call mirroring `_fetch_sync` exactly: succeeded, `0.47s` driver
creation + `1.90s` page load = `2.56s` total for `https://example.com`,
correct title and 544-byte HTML retrieved. Chromium found via
`shutil.which` at `/usr/bin/chromium-browser` (also `/snap/bin/chromium`).
Additionally ran the repo's own real end-to-end Selenium test (normally
gated behind `RUN_BROWSER_CRAWLER_TESTS=1`, confirmed skipped in a
default `pytest` run):
```
RUN_BROWSER_CRAWLER_TESTS=1 pytest tests/extra_crawlers_test.py -k selenium
tests/extra_crawlers_test.py::test_selenium_crawler_processes_page_when_available PASSED
tests/extra_crawlers_test.py::test_selenium_driver_uses_hardened_headless_flags PASSED
```
Both passed — a real `SeleniumCrawler.run()` against a real local HTTP
test server completed fetch → parse → frontier `mark_visited`
successfully.

### Selenium vs. Playwright (Selenium half of the comparison)

Both files were added in the same historical commit (matching `git log`
history) — built as parallel/sibling browser-engine options from the
start, not one superseding the other. Playwright is tried before Selenium
in the default escalation order (`["async", "playwright", "selenium",
"http"]`; `fallback_order["playwright"] = ["selenium", "http", "async"]`).
Nothing in `selenium_crawler.py` does anything Playwright's
`playwright_crawler.py` doesn't also do (both simply return rendered
HTML/`page_source` to the same shared parser). Selenium's main
differentiator is being a second, independent browser-automation
implementation for resilience if Playwright itself fails to install or
launch on a given machine — not distinct capability.

**Confidence: live-verified.**

---

## 6. Playwright diagnosis

**Status: WORKING, live-verified.**

Live diagnostic (`PlaywrightCrawler._start_browser()` → `.fetch()` →
`._stop_browser()`, exact production code path):
```
browser started in 0.29s
fetch took 1.35s, err=None, html_len=559
TOTAL 2.19s
```
Chromium binaries are installed (`~/.cache/ms-playwright`) and launch
successfully; a real fetch of `https://example.com` returned correct
HTML through the full route-interception/content-extraction path.

### Architecture

- **Browser lifecycle**: ONE shared `Browser` instance for the entire
  crawl (`_start_browser()`, `playwright_crawler.py:68-82`, called once
  from `run()` at `:332` or lazily once by
  `HybridCrawler._ensure_playwright_ready()`). Chromium launched headless
  with hardening flags (`--no-sandbox`, `--disable-dev-shm-usage`, etc.,
  `:73-82`). Stopped once at shutdown (`_stop_browser()`, `:84-90`,
  called from `run()`'s `finally` at `:347` or from
  `HybridCrawler.run()`'s `finally` at `:454-455`, **only if**
  `self._playwright_ready` was ever set `True`).
- **Context/page lifecycle**: a **new `BrowserContext` and `Page` per
  fetch attempt** (`fetch()`, `:159-194`), both explicitly closed in a
  `finally` block (`:190-194`) regardless of outcome — full browser
  process reuse (cheap, confirmed 0.29s one-time cost) combined with
  per-fetch cookie/storage isolation, materially cheaper than Selenium's
  full-relaunch-per-fetch.
- **Resource loading — actively optimized, not "load everything"**:
  `_route_request()` (`:92-119`) intercepts every request and: aborts
  `image`/`font`/`beacon` resource types outright (`:113-115`); aborts
  `media` resource-type requests unless they're a streaming manifest
  (`:108-112`, recording the media URL as evidence first if a media store
  is configured, `:94-107` — media *discovery* still happens via network
  interception even though the actual bytes aren't downloaded); aborts
  ad-domain/blacklisted request URLs (`:116-118`). This is a real,
  already-implemented cost optimization.
- **JS execution/extraction**: `page.content()` (`:169`) — a standard
  full rendered-DOM HTML snapshot after both waits below; no custom JS
  evaluation. Extraction of links/media happens downstream in the shared
  parser layer, identical to every other engine.
- **Exception handling/retry**: `fetch()`'s loop (`:158-196`) catches
  `PlaywrightError` specifically and a broad `Exception` fallback, both
  logged via `logger.warning` and retried with 1s sleep, always
  surfacing `(None, error_string)` on exhaustion. `asyncio.CancelledError`
  is explicitly re-raised, not swallowed (`:180-181`).
- **Concurrency**: inside `HybridCrawler`,
  `self._playwright_semaphore = asyncio.Semaphore(max(1, min(2,
  self.concurrency)))` (`hybrid_crawler.py:92`) — **at most 2 concurrent
  Playwright fetches**. Standalone `--crawler-engine playwright`
  self-caps at `min(concurrency, 8, max_pages)`
  (`playwright_crawler.py:44`) — not the full configured 25, mirroring
  Selenium's own self-imposed cap.
- **Process-leak risk**: `_stop_browser()` only runs from `finally`
  blocks in both `PlaywrightCrawler.run()` and `HybridCrawler.run()` —
  normal `try/finally`, which does not run on a hard `SIGKILL`/OOM-kill.
  A graceful `SIGTERM`/`Ctrl-C` shutdown correctly reaches the `finally`
  and closes the browser; an unclean kill would leak the Chromium
  process(es) — generic to any browser-automation tool under an unclean
  kill, not specific to this code.

### Timeout finding — the clearest compounding case in this audit

`fetch()` (`:167-168`):
```python
response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
await page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
```
Two **sequential** waits, each individually allowed up to the full
configured `timeout` (15s in the default Hybrid/shared config). Worst
case for one attempt: `goto` up to 15s, then
`wait_for_load_state("networkidle")` **another** up to 15s — ~30s for a
single attempt, not 15s as the config value alone suggests. With
`max_retries` (2 own default / typically 3 via Hybrid), a single URL that
never settles could occupy a Playwright slot for on the order of
60-90+ seconds before being marked failed. Neither number alone looks
alarming — this compounds silently.

### Playwright vs. Selenium (Playwright half of the comparison)

Both added in the same historical commit, offering no distinct
*capability* over each other in this codebase — neither does anything
(JS execution, extraction) the other's code path doesn't also do. The
real architectural difference is cost/reuse shape: Playwright reuses one
shared browser process across the whole run (cheap), while Selenium
relaunches a full Chrome process per fetch (expensive) — correspondingly
Playwright is tried first and gets a higher concurrency allowance (2 vs.
1 in Hybrid; 8 vs. 4 self-capped standalone).

### Test coverage note

Real end-to-end Playwright/Selenium tests exist
(`tests/extra_crawlers_test.py::test_playwright_crawler_processes_page_when_available`
and the Selenium equivalent) but are **gated behind
`RUN_BROWSER_CRAWLER_TESTS=1`**, skipped in a default `pytest` run — real
browser execution is not exercised by ordinary CI/test runs.
`tests/hybrid_crawler_test.py:55,69` test the router's `get_engine_plan()`
ordering itself with mocked engines (confirms
`["async", "scrapling", "playwright", "selenium"]` as the real default
order) — router *logic* is tested, real browser *execution* is not, by
default.

**Confidence: live-verified.**

---

## 7. HTTP vs. browser decision flow

**Verdict: the "cheap HTTP first, browser only if needed" strategy is
already implemented — with one concrete, verified gap.**

`core/crawler_router.py::get_engine_plan(url, current_engine=None)`
(`:141-146`) returns `["async", (scrapling), "playwright", "selenium",
"http"]` for a fresh URL — `AsyncCrawler` (aiohttp, the cheapest option)
is tried first, every time, for every non-onion URL. Escalation to
Playwright only happens when `needs_browser_upgrade()` (`:85-127`) says
so: either the initial fetch's `failure_reason` matches an anti-bot/
JS-required token set (captcha, cloudflare, 403/401/429, "verify you are
human", etc., `:96-113`), or the fetched HTML itself matches JS-required
markers (`data-reactroot`, `__next_data__`, `cf-browser-verification`, a
generic script-heavy/anchor-sparse heuristic, `:118-126`). This is wired
into `HybridCrawler._run_engine_plan()` (`:218-274`) — after "async"
succeeds but `needs_browser_upgrade(url, html=html)` says the content
needs rendering, it discards that HTML and re-plans toward Playwright
(`:245-259`). **This is exactly the "cheap fetch → check sufficiency →
escalate" strategy, already built.**

### Gap found — `prefers_browser()` is dead code, never wired in

`core/crawler_router.py:59-75` defines `prefers_browser(url)`, which
checks for known JS-heavy URL *path* patterns
(`JS_HEAVY_PATH_TOKENS = {"/watch", "/stream", "/player", "/embed",
"/play", "/episode", "/movies", "/series"}`, `:11-20`) and
piracy-domain-classified watch/stream/play/download paths — exactly the
"we already know this URL is going to need a browser, don't waste an
HTTP round-trip first" pre-fetch shortcut. **Confirmed by grep
(`grep -rn "prefers_browser"`): zero callers anywhere in the codebase
outside its own definition.** Neither `HybridCrawler._run_engine_plan()`
nor `get_engine_plan()` itself consults it. Today, even a URL that is
unambiguously going to need a browser by its path shape alone (e.g.
`/watch/some-movie`) still pays for one wasted `AsyncCrawler` HTTP
attempt first — a heuristic that looks wired-in from its presence in the
class but isn't actually consulted anywhere.

### Fetch-method comparison table

| Fetch method | Trigger condition (default plan) | Expected use case | Approx. cost | Timeout | Retry | Async-safe? | Working? | Failure visibility |
|---|---|---|---|---|---|---|---|---|
| **AsyncCrawler** (`aiohttp`) | 1st, every non-onion URL | Cheap default HTTP, no JS | Sub-second for simple pages (not independently live-timed; aiohttp is lightweight) | `crawler.timeout` (15s), no connect/read split | ≤`max_retries` (3), only 5xx/exception, 1s sleep | Yes — native async, no blocking calls | Yes (code-verified) | `mark_failed` + `logger.warning`/`.error`; typed reason string |
| **HTTPCrawler** (`httpx`) | **Last** resort — despite the generic name | Fallback HTTP stack, distinct connection/TLS fingerprint from aiohttp | Comparable to AsyncCrawler | 15s | Same 3-attempt/5xx pattern | Yes — native `httpx.AsyncClient` | Yes (code-verified) | Same pattern |
| **PlaywrightCrawler** | 2nd, or on `needs_browser_upgrade()` | JS-rendered pages, anti-bot pages needing a real browser | Live: 0.29s one-time browser start + 1.35s/fetch (trivial page; real pirate-site pages un-benchmarked) | 15s **×2 sequential** (§6) | 2 (own)/3 (Hybrid), 1s sleep | Yes — `playwright.async_api`, no blocking calls | Yes, live-verified | Same pattern; `PlaywrightError` caught specifically |
| **SeleniumCrawler** | 3rd, after Playwright | Fallback browser if Playwright unavailable | Live: 0.47s create + 1.90s fetch = 2.56s, **per-fetch, no reuse** | 15s (overrides own 30s default via Hybrid/Manager) | 2 (own)/3 (Hybrid), 1s sleep | Yes — `to_thread`-wrapped | Yes, live-verified | Same pattern; `driver.quit()` cleanup silent |
| **ScraplingCrawler** | Optional 2nd (before Playwright) when `scrapling_enabled: true` (default) | Stealth/anti-detection fetch for anti-bot-protected surface-web pages; explicitly refuses onion URLs (`scrapling_crawler.py:112`) | Not independently live-tested (out of explicit audit scope) — code-read only, same fetch/retry/error shape as other engines | shared `crawler.timeout` presumably | Own retry loop, same pattern | Appears async-safe on a skim, not independently confirmed | **UNVERIFIED** (code-only) | Same `mark_failed` pattern |
| **Tor-proxied HTTP** (`TorCrawler`) | Only engine ever tried for `.onion` URLs; no fallback | Dark-web fetch | Live: real `.onion` fetch succeeded (§4) | 15s via Hybrid (20s class default unused) | ≤3, 5xx/exception only, 1s sleep | Yes — native async `httpx.AsyncClient` | Yes, live-verified | Same pattern |

**Confidence: live-verified for AsyncCrawler's position and the
escalation trigger logic (code-read + confirmed by
`tests/hybrid_crawler_test.py`'s mocked-order assertions); live-verified
for Playwright/Selenium/Tor's actual fetch behavior; code-only/UNVERIFIED
for Scrapling.**

---

## 8. Async / blocking audit

**No blocking-call defect was found in the fetch engines themselves** —
a genuine positive finding. Selenium's and Scrapling's `fetch()` methods
both wrap every call (not just one-time readiness checks) in
`asyncio.to_thread`, matching the pattern already established for the
frontier's synchronous `redis-py` client
(`core/frontier_executor.py:70-73`, per
`docs/architecture/system-architecture.md` §7). No engine touches Redis
directly or blocks the loop on its own fetch logic.

### Finding — Media Evidence writes are synchronous and unwrapped on the event loop (the one real finding here)

Every crawler engine's `worker()` calls
`self.media_database.record_media_link(...)` directly, with **no
`await`, no `asyncio.to_thread`** — confirmed by grep across all six
engines: `hybrid_crawler.py:314`, `async_crawler.py:194`,
`playwright_crawler.py:132/235`, `tor_crawler.py:99/164`,
`http_crawler.py:93/158`, `selenium_crawler.py:181`.

`storage/redis_media_evidence_store.py:514` — `record_media_link` is a
plain synchronous `def` (not `async def`), executing Lua scripts through
a **synchronous `redis-py` client** (`self.redis_conn`, constructed and
`.register_script()`'d synchronously at `:123-139`, used throughout via
plain `self.redis_conn.<call>` — no `redis.asyncio`, no `to_thread`
anywhere in the file). `core/crawler_manager.py:126`:
```python
self.media_database: Optional[MediaEvidenceStore] = build_media_evidence_store(self.config)
```
— the raw store object is handed directly to every crawler engine, with
**no equivalent of `AsyncFrontier`/`core/frontier_executor.py`'s
`asyncio.to_thread` offload wrapper** for Media Evidence calls made from
the crawl-time hot path. The frontier's own synchronous `redis-py` client
was deliberately wrapped this way specifically so the event loop never
blocks on Redis I/O (architecture doc §7) — **that same reasoning applies
verbatim to `record_media_link`/`record_manifest_variants`, but the
wrapping was never applied to them.**

- **Call path**: `HybridCrawler.worker()` (or any single-engine worker) →
  (inline, synchronous, no `await`) →
  `RedisMediaEvidenceStore.record_media_link()` → `self.redis_conn.<Lua
  script call>` (synchronous socket I/O to Redis).
- **Why it blocks**: a synchronous `redis-py` call performs real network
  I/O while holding the single OS thread the asyncio event loop runs on
  — every other concurrently-scheduled coroutine in the process (all
  other in-flight fetches across all engines, the scheduler task, the
  recovery task) is frozen for the duration of that one Redis round trip.
- **Frequency/impact**: called once per media link found on a page
  (0 to many times per page — `for media in media_links:` loops in every
  engine's `worker()`) plus once more for `record_manifest_variants` when
  a streaming manifest is parsed (`async_crawler.py:122-124`,
  `http_crawler.py:104-106`, `tor_crawler.py:110-112`,
  `playwright_crawler.py:143-146`). On a media-dense piracy site (the
  crawler's actual target domain), this is not a rare edge case. Each
  individual round trip against local Redis is likely small (sub-ms to
  low-ms), but is fully serialized with respect to every other concurrent
  async fetch — under `crawler.concurrency: 25` this directly reduces
  effective fetch parallelism during those windows.
- **Live in the default `auto` path today**: yes — `HybridCrawler` wires
  `media_database` through to every sub-engine unconditionally whenever
  media evidence is enabled (default: enabled), and every engine's
  `worker()` calls it inline. This is an active, unconditional production
  code path, not a rarely-exercised branch.
- Distinct from the frontier fix already documented in this codebase's
  history (architecture doc §7's "we previously discovered the
  synchronous Redis client issue and addressed the Redis call boundary
  using to_thread" — that fix covered the frontier only, not Media
  Evidence). Not fixed here per the audit's read-only constraint.

### Narrower finding — `URLDatabase` (SQLite) calls, unwrapped but low impact in production

`self.url_database.add_url(...)` / `.update_status(...)` are called
directly (no `to_thread`) inside every engine's `worker()` (e.g.
`hybrid_crawler.py:290,294,339`), and `storage/url_database.py:19` uses
plain `sqlite3.connect(..., check_same_thread=False)` — synchronous disk
I/O on the event loop. **However**, every engine guards these calls
behind:
```python
self._sql_mode_mirror = url_database is not None and isinstance(self.frontier.raw, URLFrontier)
```
(`hybrid_crawler.py:64`) — `isinstance(..., URLFrontier)` is only `True`
for the **local, non-Redis** frontier backend. In production
(`frontier.type: "redis"`, the config.yaml default), `self.frontier.raw`
is a `RedisURLFrontier`, so `_sql_mode_mirror` is `False` and this code
path **does not execute at all** during a production/overnight Redis-mode
crawl. Only matters for local `--sql`/`frontier.type: sqlite` development
runs — a real blocking-call instance, but lower priority than the Media
Evidence finding, which fires unconditionally regardless of frontier
backend.

### Finding — claim heartbeat/renewal is correctly non-blocking

`core/claim_heartbeat.py:115-158` — wraps the fetch coroutine
(`asyncio.ensure_future(coro)`) and calls
`await frontier.renew_claim(claim)` on its own timer
(`asyncio.wait({task}, timeout=interval)` loop, `:141-149`); `frontier`
here is always an `AsyncFrontier`, itself `to_thread`-wrapped for the
Redis backend. Confirmed correct — no blocking call.

### Finding — parser layer is synchronous/CPU-bound, called inline; not a demonstrated problem

`parsers/html_link_extractor.py`, `parsers/media_link_detector.py`,
`parsers/streaming_manifest_parser.py`, `parsers/javascript_link_extractor.py`
are all plain synchronous functions (BeautifulSoup/lxml parsing, regex
over page text/scripts) called inline (no `await`, no `to_thread`) from
every engine's `worker()` right after a successful fetch
(`hybrid_crawler.py:301-305`). This does block the event loop for parse
duration, same category as the findings above, but:
- No evidence of pathologically expensive parsing (no nested/backtracking
  regex found on a quick read; bounded `soup.find_all`/selector passes).
- Runs once per successfully-fetched page (not once per media link like
  the primary finding), so lower call frequency.
- Could still matter for unusually large pages (multi-MB HTML dumps are
  not unheard of on ad-heavy piracy sites) — `BeautifulSoup(html, "lxml")`
  parse time scales with document size and is not bounded by any size
  cap found in this codebase. Flagged as **plausible, not confirmed** —
  no benchmark exists today to say whether this is significant at
  real-world page sizes (see §18).

### Grep sweep for other blocking primitives

No `subprocess`/`os.system` calls found anywhere in `crawler/*.py` or
`parsers/*.py`. No direct blocking file I/O (`open()`) found in the
fetch/extractor hot path.

**Confidence: live-verified that Selenium/Scrapling are correctly
thread-offloaded (grep + code read); code-verified (not live-load-tested)
for the Media Evidence blocking finding's real-world throughput impact —
the mechanism is confirmed, the magnitude of impact under real
concurrency was not benchmarked (see §18).**

---

## 9. Timeout inventory

| Component | Timeout value | Where configured | Notes |
|---|---|---|---|
| Search engines (all 6) | 15s | `config.yaml:99` (`search.timeout`) → `search_engines/base.py:36-37` | Single httpx timeout, no connect/read split, **no retry** at all (one shot per engine per query) |
| AsyncCrawler (aiohttp) | 15s | `config.yaml:4` (`crawler.timeout`) → `crawler/async_crawler.py:26,87` | ≤3 retries, only 5xx/exception, 1s sleep — worst case ~3×15s+2×1s ≈ 47s for one URL across all attempts |
| HTTPCrawler (httpx) | 15s | same `crawler.timeout` → `crawler/http_crawler.py:29` | Same retry shape as AsyncCrawler |
| Tor-proxied HTTP (`TorCrawler`) | 15s (via Hybrid/Manager explicit `timeout=` kwarg) — class's own unused default is 20s (`tor_crawler.py:30`) | `crawler.timeout` | Retries only on 5xx/exception, same 1s sleep pattern; class-level 20s default is dead weight, never actually applied |
| **Playwright navigation** | **15s applied twice sequentially** (`goto(wait_until="domcontentloaded")` then `wait_for_load_state("networkidle")`, both `timeout=self.timeout*1000`) | `crawler.timeout` → `crawler/playwright_crawler.py:167-168` | **Suspicious/compounding** — effective worst-case per attempt is ~2× the configured timeout (~30s), not 15s; across retries (2 own default/3 via Hybrid), worst case for one URL is ~60-90s+ |
| Selenium page load | 15s (via Hybrid/Manager explicit `timeout=`) — class's own unused default is 30s (`selenium_crawler.py:36`) | `crawler.timeout` → `driver.set_page_load_timeout(self.timeout)` (`:106`) | Same "class default never actually applies" pattern as Tor's 20s; retries each pay full driver-relaunch cost on top of any page-load wait |
| Claim lease (`lease_ttl`) | 90s | `config.yaml` (`crawler.frontier.lease_ttl`) | Independent of fetch timeouts — the heartbeat mechanism decouples these, so slow fetches don't lose their claim as long as heartbeats keep landing |
| Heartbeat interval | `lease_ttl / 3` = 30s by default (`core/claim_heartbeat.py:66-76`), clamped below `lease_ttl/2` if explicitly configured (`:79-98`) | derived, not directly configured | Correctly decoupled from any single fetch's timeout — the Playwright double-timeout compounding above does **not** threaten claim ownership, only wall-clock throughput per URL |
| Frontier retry backoff | `base_backoff`=5.0s, `max_backoff`=300.0s, `max_retries`=3 | `config.yaml:20-22` | Layered **on top of** each engine's own internal per-attempt retry loop — total attempts before permanent failure ≈ engine-internal-retries × frontier-max-retries in the worst case, usually fewer since router escalation changes engines between frontier-level retries |
| Selenium/Playwright/Tor readiness checks | n/a (one-shot, not a fetch) | `hybrid_crawler.py` `_ensure_playwright_ready`/`_ensure_selenium_ready` | Uses each engine's own implicit timeout — an unresponsive `_start_browser()`/driver init has no explicit timeout wrapper of its own (not observed live; both launched in under 0.5s here) |
| `blocked_engine_cooldown_queries` | 999 | `config.yaml:108` | De-facto "disable for the rest of the run," no rationale comment in config, unlike other tuned constants nearby |

**Biggest red flag**: Playwright's double-sequential-timeout
(`goto` + `wait_for_load_state("networkidle")`, each independently
allowed the full configured timeout) is the clearest "timeout multiplied
silently" case in the inventory.

**Confidence: live-verified for Playwright/Selenium/Tor/search-engine
timeout values (read directly from the constructors and confirmed
against config.yaml); code-verified for the frontier/heartbeat/backoff
values (documented and cross-checked, not independently re-derived by
live test).**

---

## 10. Error-visibility findings

**General pattern (confirmed consistent across all six crawler engines
by reading every `except Exception` block in `crawler/*.py`): no bare
"catch and silently continue" pattern was found in the fetch/worker hot
path.** Every fetch-level exception is caught, converted to a
`(None, error_string)` tuple (or re-raised for `CancelledError`), logged
via `logger.warning`/`.error` at the point of failure, and ultimately
reaches `frontier.mark_failed(claim, failure_reason)` — so a failure is
simultaneously: (a) logged, (b) attached to a specific error string, not
just "failed", (c) recorded against the URL's claim in frontier state,
(d) subject to the frontier's own retry/backoff. Holds for
async_crawler, http_crawler, tor_crawler, playwright_crawler,
selenium_crawler, scrapling_crawler, and hybrid_crawler's engine-plan
escalation loop alike.

### Two narrower exceptions, both debug-level-only or fully silent

1. **Media-evidence-record failures are debug-level-only** — identical
   `except Exception as exc: logger.debug(f"Skipping media evidence
   capture for {url}: {exc}")` in every engine (e.g.
   `hybrid_crawler.py:324-325`, `async_crawler.py:204-205`). A failure to
   record a *discovered media link* (as opposed to a page-fetch failure)
   is deliberately non-fatal to the page crawl (reasonable — one bad
   media record shouldn't fail the whole page) but is logged only at
   `debug`, which most production log configurations filter out. **This
   is the closest thing in the codebase to the "component appears to work
   while silently failing" pattern this audit was watching for** — not
   because the exception is swallowed (it's caught and logged), but
   because at `warning`/`info`-level production logging (the actual
   default — `core/crawler_manager.py:121` configures logging at `INFO`),
   media-evidence-record failures would be invisible even though page
   crawling itself succeeds and reports as normal.
2. **`SeleniumCrawler._fetch_sync`'s `driver.quit()` cleanup**
   (`selenium_crawler.py:124-128`): `except Exception: pass` — fully
   silent, no log at all. Narrow in scope (only covers the cleanup call,
   not the fetch itself, which is separately and fully error-visible),
   but a genuine blind spot: a `driver.quit()` failure pattern (e.g.
   repeated zombie Chrome processes) would produce zero log signal,
   observable only via OS-level process accounting. This is the only
   fully-silent exception handler found anywhere in this entire sweep.

Search engines: covered in §2/§3 — `SearchEngineError` subclasses are
caught, logged, and recorded per-engine in `report.engine_errors`, not
silently swallowed; Yandex's block is correctly surfaced as a typed
`SearchEngineBlockedError`. Tor: covered in §4 — same
`mark_failed`+logging pattern as every other engine, fully visible.

### Would `tests/report.py`/`tests/report_lib.py` surface any of this?

Per-URL failure reasons are **not** aggregated into the JSON run report
(`--output` flag) today — `report_lib.build_report()` reports frontier
status *counts* (queued/visited/failed_permanent/etc. via
`get_status_counts()`) but not failure-reason breakdowns or per-engine
failure attribution. An operator reviewing a run report would see "N
URLs failed" but would need to grep loguru logs (or query frontier state
directly) to learn *why*, or which engine was responsible. This also
independently reconfirms the already-known, pre-existing code bug noted
in `docs/architecture/system-architecture.md` §25: `tests/report.py
--redis` queries Redis keys (`{ns}:urls:queued`, `{ns}:urls:failed`) that
do not exist in the current frontier keyspace and will always report zero
for those two fields against a real production frontier.

**Confidence: live-verified for the general no-silent-swallow pattern
(read every relevant `except` block directly); code-verified for the
`report.py`/`report_lib.py` limitation.**

---

## 11. Resource / cost findings

Not a benchmark — an inventory of what resource-cost data is measurable
today versus what is missing, ahead of the overnight run.

### Already exists and is wired in

`tests/benchmarks/common.py::ResourceMonitor` (used via `main.py
--monitor-resources`, wired at `main.py:227-232`) — a background-thread
`psutil`-based sampler: process CPU%, RSS, and (`include_children=True`)
**aggregate** child-process CPU/RSS + child-process **count**
(`_sample_children`, `common.py:306-337`). Also samples Redis `INFO`
(connected clients etc., `:358-364`) when a `redis_conn` is supplied.
This is real, working instrumentation, confirmed present and wired into
the CLI — not aspirational.

### What it does NOT provide

- No per-engine breakdown — a Playwright Chromium process and a Selenium
  Chrome process are indistinguishable in the `children_*` aggregate;
  both are just "child process CPU/RSS," with no tagging of which child
  PID belongs to which engine.
- No per-fetch timing anywhere in the fetch/extractor path
  (`time.monotonic()` grepped across `crawler/*.py` — not found); fetch
  duration is not currently measured or logged per-URL, only implied by
  overall throughput in the final "processed=N" summary log line.
- Tor connection latency: **no instrumentation found** — nothing times
  the Tor SOCKS handshake or any Tor-proxied request specifically; would
  need to be added by timing `TorCrawler.fetch()` calls (currently
  un-timed).
- Browser startup/context/page creation cost: **no instrumentation found**
  in the actual crawler code — the only startup-cost numbers in this
  audit (Playwright: 0.29s browser start; Selenium: 0.47s driver create)
  came from this audit's own ad hoc live diagnostics, not from the
  crawler itself recording them during a real run.
- Search-engine latency/failure-rate/result-count: result counts and
  error strings are captured in `QueryDiscoveryReport` (§2), but latency
  is not, and none of it is persisted into the JSON run report today —
  only to loguru text logs.

**Bottom line for planning the overnight run**: process-level CPU/RSS and
Redis INFO are already measurable via `--monitor-resources`; anything
more granular (per-engine cost attribution, per-fetch timing, browser
process counts by engine, Tor latency) does not exist in the codebase
today and would need to be added in a future controlled implementation
phase before it could be measured (see §16).

**Confidence: live-verified that `ResourceMonitor` exists and is wired
(read the code + `main.py`'s CLI wiring directly); the "does not exist"
items are confirmed by grep, not by attempting to run a full instrumented
crawl (out of scope for this audit).**

---

## 12. Failure matrix

| Component | Status | Failure point | Root cause | Blocking? | Cost |
|---|---|---|---|---|---|
| DuckDuckGo | WORKING | — | — | No | Low |
| Bing | WORKING | `site:` operator query only | Bing UX layout quirk, not a parser bug | No | Low |
| Brave | ENVIRONMENT-DEPENDENT | HTTP 429 (1 sample) | Unknown — needs repeated sampling | No | Low |
| Yandex | BROKEN (externally) | captcha wall | Yandex anti-bot, confirmed live | No (isolated per-engine) | Low |
| Ahmia | WORKING | — | — | No | Low |
| Torch | WORKING | — | — | No | Low |
| Tor fetch path | WORKING | env-dependent | Needs live Tor daemon; no explicit "Tor down" signal if absent | No | Low-Med (browser-free) |
| Selenium | WORKING | — | — | No (`to_thread`) | **High** (full relaunch/fetch) |
| Playwright | WORKING | — | — | No | Medium (shared browser, capped concurrency) |
| AsyncCrawler/HTTPCrawler | WORKING (code-verified) | — | — | No | Low |
| Scrapling | UNVERIFIED | — | Not live-tested this pass | Unconfirmed | Unconfirmed |
| Media Evidence writes | **PARTIALLY WORKING** | blocks event loop | Missing `to_thread` wrap, unlike the frontier | **Yes** | Throughput tax, frequency scales with media density |
| `prefers_browser()` routing | BROKEN (dead code) | never called | Logic exists, never wired in | No | Wasted HTTP round-trips on known-JS-heavy URLs |
| `tests/report.py --redis` | BROKEN (known, pre-existing) | wrong Redis keys | Already documented in architecture doc §25 | No | Reporting blind spot only |

No component qualifies as **P0** (prevents crawling/discovery outright)
— every "broken" item fails in isolation without stalling the rest of
the pipeline.

---

## 13. P0/P1/P2/P3 priorities

Severities as originally assessed — not adjusted in this write-up.

### P0 — none found.
Nothing prevents actual crawling/discovery. Every component that's
"broken" (Yandex) fails in isolation without stalling the rest of the
pipeline.

### P1 — major reliability/throughput problems, live in production today
- Wrap `record_media_link`/`record_manifest_variants` calls in
  `asyncio.to_thread` (or give Media Evidence its own async adapter
  mirroring `AsyncFrontier`) — real, unconditional-in-production
  event-loop blocking, same severity class as the frontier fix (§8).
- Wire `CrawlerRouter.prefers_browser()` into the engine-plan decision so
  known-JS-heavy URLs skip the wasted first HTTP attempt (§7).

### P2 — useful improvement, crawler remains functional
- Fix Playwright's double-sequential timeout (`goto` + `networkidle`) so
  the effective worst case matches the configured value (§6, §9).
- Add per-fetch timing and per-engine resource attribution so the
  overnight run's cost data is actually collectible (§11).
- Add explicit "Tor unavailable" detection/logging instead of a silent
  proxy fallback (§4).
- Bump the media-evidence-record failure log level, or otherwise make it
  visible at normal log verbosity (§10).

### P3 — cleanup / technical debt
- Remove or repurpose the dead Tor/dark-web files (`tor_manager.py`,
  `onion_router.py`, `discovery/darkweb_discovery.py`,
  `search_engines/custom_query_generator.py`) — zero risk, pure cleanup.
- Drop the unused class-level timeout defaults on
  `TorCrawler`/`SeleniumCrawler` that are always overridden anyway, to
  avoid misleading future readers.
- Fix `tests/report.py --redis`'s stale key names (already known,
  documented).

---

## 14. Recommended future fixes

In priority order, matching §13:

1. Add an async offload boundary for Media Evidence writes, structurally
   mirroring `core/frontier_executor.py`'s `AsyncFrontier` — likely a
   thin wrapper class around `MediaEvidenceStore` that runs
   `record_media_link`/`record_manifest_variants` via `asyncio.to_thread`,
   constructed once in `core/crawler_manager.py` alongside
   `build_media_evidence_store`, and threaded through every engine's
   `common_args` the same way the frontier already is.
2. Wire `CrawlerRouter.prefers_browser()` into `get_engine_plan()` (or
   into `HybridCrawler._run_engine_plan()`'s initial-plan branch) so a
   URL matching known JS-heavy path tokens starts directly at Playwright
   instead of paying for a doomed `AsyncCrawler` attempt first.
3. Change Playwright's `fetch()` to budget the two waits (`goto` +
   `wait_for_load_state`) against one shared deadline instead of each
   getting the full `self.timeout` independently.
4. Add `time.monotonic()`-based per-fetch timing (start/end around each
   engine's `fetch()` call) and thread it into either the existing
   per-page log line or a new structured counter, so overnight-run cost
   data doesn't require re-instrumenting later.
5. Add a run-level "Tor unavailable" signal (mirroring Yandex's named
   cooldown) instead of relying on per-URL connection-refused exceptions
   to imply the same thing.
6. Raise the media-evidence-record failure log level above `debug`, or
   otherwise surface it distinctly from ordinary page-fetch failures.
7. Distinguish "Yandex failed once for an unrelated reason" from "Yandex
   is captcha-walled" in the cooldown-trigger logic
   (`discovery/search_engine_discovery.py:256`).
8. Remove the confirmed-dead files (`tor/tor_manager.py`,
   `tor/onion_router.py`, `discovery/darkweb_discovery.py`,
   `search_engines/custom_query_generator.py`) and the unused
   class-level timeout defaults on `TorCrawler`/`SeleniumCrawler`.
9. Fix `tests/report.py --redis`'s stale key names (pre-existing,
   already documented in architecture doc §25).

---

## 15. Exact files/functions involved

| Fix | File(s) | Function(s) |
|---|---|---|
| Media Evidence async offload | `core/frontier_executor.py` (pattern reference), a new module (e.g. `core/media_evidence_executor.py`), `core/crawler_manager.py`, all six `crawler/*.py` call sites | `AsyncFrontier` (reference), `build_media_evidence_store`, every `worker()`'s `record_media_link`/`record_manifest_variants` calls |
| Wire `prefers_browser()` | `core/crawler_router.py`, `crawler/hybrid_crawler.py` | `CrawlerRouter.get_engine_plan()`, `HybridCrawler._run_engine_plan()` |
| Playwright shared timeout budget | `crawler/playwright_crawler.py` | `PlaywrightCrawler.fetch()` (lines ~167-168) |
| Per-fetch timing instrumentation | all `crawler/*.py`, `crawler/hybrid_crawler.py` | each engine's `fetch()`, `HybridCrawler._fetch_with_engine()` |
| Tor-unavailable run-level signal | `tor/proxy_config.py`, `crawler/hybrid_crawler.py` or `core/crawler_manager.py` | `get_default_tor_proxy()`, wherever a run-level health flag would live |
| Media-evidence log level | all `crawler/*.py` | the `except Exception as exc: logger.debug(...)` blocks around `record_media_link` |
| Yandex cooldown granularity | `discovery/search_engine_discovery.py` | the `blocked_engines[engine_name] = ...` block (~line 256) |
| Dead-code removal | `tor/tor_manager.py`, `tor/onion_router.py`, `discovery/darkweb_discovery.py`, `search_engines/custom_query_generator.py` | whole files |
| `tests/report.py --redis` key fix | `tests/report.py` | the Redis key-name constants (already documented as a known bug) |

None of these were touched during this audit.

---

## 16. What should be measured during real crawler runs

Using only what already exists (`--monitor-resources`, existing logs):
- Process-level CPU/RSS and aggregate child-process CPU/RSS/count over
  the full run (already captured).
- Redis `INFO` client-count time series (already captured, Redis mode).
- Per-query `engine_results`/`engine_errors`/`skipped_engines` from
  loguru logs (already logged, not yet in the JSON report — would need
  grep/aggregation after the run).
- Engine-usage counts from `HybridCrawler`'s final summary log line
  (`engine_usage={...}`) — already present, gives a coarse per-engine
  share of completed fetches for the run.
- Repeated, spaced-out Brave requests over the run's duration, to resolve
  whether the single 429 seen in this audit was a one-off or persistent
  rate-limiting of the crawl's egress IP.
- Whether Yandex trips the captcha wall again on first contact (expected,
  per §3) — confirms this isn't a config regression, just persistent
  external blocking.
- Frequency with which URLs actually reach Selenium/Playwright/Tor in the
  engine-plan escalation (only inferable today from the per-page log line
  `via {engine_used} chain={attempt_chain}` — would need to be grepped/
  tallied post-run, since it isn't aggregated anywhere).

Would need lightweight, non-invasive additions (not part of this
read-only audit) to get finer data:
- Per-engine child-process attribution (tag Chromium vs. Chrome PIDs).
- Per-fetch latency distribution per engine.
- Tor SOCKS handshake/request latency specifically.

---

## 17. Evidence supporting each finding

This is a consolidated log of the concrete, reproducible evidence this
audit's conclusions rest on — each live diagnostic performed, its exact
output, and the code location it verifies. Everything below was run
through the crawler's own implementation classes, one shot, and torn
down afterward.

| Finding | Evidence |
|---|---|
| Default engine is `HybridCrawler` | `config.yaml`: `crawler.engine: "auto"`; `core/crawler_manager.py:186,211` read directly — `selected_engine == "auto"` → `HybridCrawler(**hybrid_args)` |
| DuckDuckGo works | Live `engine.search("test query", max_results=5)` → real results incl. `http://www.example.com/` |
| Bing works | Live `"python programming"` query → 5 correctly base64-decoded destination URLs (python.org, w3schools, etc.); 10 anchors matched under `li.b_algo h2 a[href]` |
| Bing `site:` quirk | Live `site:example.com` query → 0 results under the same selector (layout difference, not exception) |
| Brave 429 | Live `engine.search(...)` → `SearchEngineUnavailableError: HTTP 429 from brave`, single sample |
| Yandex blocked | Live `YandexSearch().search("test query")` → `SearchEngineBlockedError`; replicated raw request redirects to `https://yandex.com/showcaptcha?cc=1&form-fb-hint=1.1&mt=...`, page title `Are you not a robot?` |
| Ahmia works | Live `search("market")` → 5 real `.onion` URLs |
| Torch works, Tor SOCKS live | Live `search("market")` → 5 real `.onion` URLs through the first of 3 hardcoded mirrors |
| Tor daemon running in this environment | `ps aux` shows `/usr/bin/tor --defaults-torrc ... -f /etc/tor/torrc`; raw socket probe `_is_local_port_open('127.0.0.1', 9050)` → `True` |
| Real `.onion` fetch via `TorCrawler.fetch()`'s exact code path | `httpx.AsyncClient(timeout=20, follow_redirects=True, proxy="socks5h://127.0.0.1:9050")` → `status: 200  len: 7693  onion? True` |
| Dead code: `tor_manager.py`, `onion_router.py`, `darkweb_discovery.py`, `custom_query_generator.py` | `grep -rn` for `TorManager`, `OnionRouter`, `darkweb_discovery`/`load_onion_seeds`, `CustomQueryGenerator`/`custom_query_generator` — each matches only its own definition, zero other importers |
| Selenium driver launches and fetches | Live `_make_driver()` → `0.47s`; `driver.get('https://example.com')` → `1.90s`; total `2.56s`; 544-byte HTML + correct title retrieved; Chromium found at `/usr/bin/chromium-browser` / `/snap/bin/chromium` via `shutil.which` |
| Selenium `fetch()` is thread-offloaded | `crawler/selenium_crawler.py:130-142` read directly — `await asyncio.to_thread(self._fetch_sync, url)` |
| Real Selenium end-to-end test passes (normally skipped) | `RUN_BROWSER_CRAWLER_TESTS=1 pytest tests/extra_crawlers_test.py -k selenium` → both `test_selenium_crawler_processes_page_when_available` and `test_selenium_driver_uses_hardened_headless_flags` PASSED |
| Playwright browser launches and fetches | Live `_start_browser()` → `0.29s`; `.fetch('https://example.com')` → `1.35s`, `err=None`, `html_len=559`; total `2.19s`; Chromium binaries present at `~/.cache/ms-playwright` |
| Playwright double timeout | `crawler/playwright_crawler.py:167-168` read directly — two sequential `await` calls each with `timeout=self.timeout * 1000` |
| `prefers_browser()` dead code | `grep -rn "prefers_browser"` across the repo — only match is its own definition in `core/crawler_router.py:59` |
| Default engine plan order | `core/crawler_router.py:141-146` read directly; cross-checked against `tests/hybrid_crawler_test.py:55,69`'s mocked-engine assertions of `["async", "scrapling", "playwright", "selenium"]` |
| Media Evidence calls are unwrapped sync calls | `grep` across all six `crawler/*.py` files for `record_media_link`/`record_manifest_variants` call sites — none preceded by `await`/`to_thread`; `storage/redis_media_evidence_store.py:514` confirmed as a plain synchronous `def` using a synchronous `redis-py` client |
| `_sql_mode_mirror` gates SQLite calls out of production | `hybrid_crawler.py:64` read directly — `isinstance(self.frontier.raw, URLFrontier)`, `False` for `RedisURLFrontier` (the config.yaml default) |
| Media-evidence failures logged at `debug` only | Identical `logger.debug(f"Skipping media evidence capture for {url}: {exc}")` found in every engine, e.g. `hybrid_crawler.py:324-325`, `async_crawler.py:204-205`; production logging confirmed configured at `INFO` (`core/crawler_manager.py:121`) |
| Selenium `driver.quit()` is the only fully-silent handler | `selenium_crawler.py:124-128` read directly — `except Exception: pass`, no log call in the block |
| `report.py --redis` key mismatch | Pre-existing, documented in `docs/architecture/system-architecture.md` §25; independently reconfirmed by reading `tests/report.py`'s key-name constants against the current `RedisURLFrontier` keyspace |
| `ResourceMonitor` exists and is wired | `tests/benchmarks/common.py::ResourceMonitor`; `main.py:227-232` confirms `--monitor-resources` constructs and enters/exits it around `manager.run()` |
| `git` diff unaffected by this audit | `git status --short` / `git diff --stat` before and after this investigation are identical to the pre-existing uncommitted state (see §18 and the original session's starting `gitStatus`) |

---

## 18. Uncertainty / unverified behavior

Everything in this section is explicitly **not** confirmed to the same
standard as the rest of the report — flagged here so it isn't mistaken
for a settled finding.

- **Brave's HTTP 429** was observed exactly once, from this sandbox's
  single IP, on the first request. Whether this reflects persistent
  rate-limiting of this environment's egress IP or a one-off transient
  block is **UNKNOWN** — repeated, spaced-out sampling was deliberately
  not performed here to avoid hammering Brave's servers. Classified as
  ENVIRONMENT-DEPENDENT, not BROKEN.
- **ScraplingCrawler** was explicitly out of this audit's live-testing
  scope. Its async-safety, real fetch behavior, and failure modes are
  **UNVERIFIED** — code-read only, no live diagnostic was run against it.
- **Tor-unavailable behavior** (what happens when no Tor daemon is
  running at all) was not observed live in this environment, because a
  real Tor daemon happened to be running. The code path for this case
  (silent fallback to `socks5h://127.0.0.1:9050` even if nothing is
  listening, per `tor/proxy_config.py:41`) was read but not exercised —
  its actual failure signature (generic connection-refused/timeout
  exception per URL) is inferred from the code, not observed.
- **Selenium's fixed `--remote-debugging-port=9222`** was flagged as a
  theoretical collision risk if two concurrent driver instances launch on
  the same machine — this was **not independently verified** beyond the
  single-instance live test performed here; in practice Selenium 4's own
  driver management may assign each instance its own port regardless of
  this flag, but that behavior was not directly tested.
- **The magnitude of the Media Evidence blocking finding's real-world
  throughput impact** (§8) is a confirmed mechanism (a synchronous Redis
  call runs directly on the event loop, unconditionally, once per media
  link) but its actual effect on overall crawl throughput under real
  `concurrency: 25` load was **not benchmarked** — the finding is about
  the presence of the blocking call, not a measured slowdown percentage.
- **Parser-layer (BeautifulSoup/lxml) cost on unusually large pages** is
  flagged as plausible but **unconfirmed** — no size cap exists in the
  codebase and no benchmark was run against a real multi-MB piracy-site
  page in this audit.
- **Browser-process-leak risk under a hard `SIGKILL`/OOM-kill** (both
  Playwright and Selenium) is a structural observation from reading the
  `try/finally` shutdown code, not something reproduced live — an actual
  hard-kill-during-fetch scenario was not simulated.
- **Search-engine "recently verified" status** in the strict sense
  requested by the original audit brief (evidence of routine, ongoing
  verification, e.g. in CI) does not exist for any engine —
  `tests/search_engine_test.py` covers only isolated parsing-logic units
  (e.g. Bing's base64 decode) with hardcoded strings, not live `search()`
  calls; the DuckDuckGo/Bing/Ahmia/Torch "WORKING" verdicts in this audit
  rest entirely on this audit's own one-time live diagnostics, not on
  any pre-existing, repeatable verification the codebase performs on its
  own.
- **Whether `TorCrawler`'s fixed `--remote-debugging-port`-style
  collision concerns extend to concurrent multi-machine production
  deployments** was not assessed — this audit ran against a single
  local process, not the multi-machine Redis-coordinated fleet described
  in `docs/architecture/system-architecture.md` §13.

---

## Next Actions

Recommended implementation order for the next controlled phase, carried
forward unchanged from §13/§14 (P1 before P2 before P3; nothing is P0):

1. **Wrap Media Evidence writes in `asyncio.to_thread`** (or a dedicated
   async adapter mirroring `AsyncFrontier`) — highest-priority fix; it is
   live, unconditional event-loop blocking in production today.
2. **Wire `CrawlerRouter.prefers_browser()` into the engine-plan
   decision** so known-JS-heavy URLs skip the wasted first HTTP
   round-trip — the logic already exists, it only needs to be called.
3. **Fix Playwright's doubled navigation timeout** (`goto` +
   `wait_for_load_state("networkidle")` sharing one deadline instead of
   each getting the full configured timeout).
4. **Add per-fetch timing instrumentation** across all engines so the
   next real crawl run produces the cost data §16 describes, without
   needing to re-instrument mid-run.
5. **Add a run-level "Tor unavailable" signal**, mirroring Yandex's named
   cooldown, instead of relying on per-URL connection exceptions to imply
   the same thing.
6. **Raise the media-evidence-record failure log level** above `debug`
   so it is visible at normal production verbosity.
7. **Give Yandex's cooldown trigger failure-type granularity** so a
   transient error doesn't read identically to a confirmed captcha block
   in the logs.
8. **Remove the confirmed-dead files** (`tor/tor_manager.py`,
   `tor/onion_router.py`, `discovery/darkweb_discovery.py`,
   `search_engines/custom_query_generator.py`) and the unused
   class-level timeout defaults on `TorCrawler`/`SeleniumCrawler`.
9. **Fix `tests/report.py --redis`'s stale key names** (pre-existing,
   already documented in architecture doc §25 — bundle with the above
   cleanup pass rather than treating as urgent on its own).

Before implementing any of the above, resolve the open uncertainty items
in §18 that bear directly on them where practical (in particular:
confirm Brave's 429 behavior over repeated sampling before deciding
whether it needs its own fix, and benchmark the Media Evidence blocking
finding's actual throughput impact under real concurrency to size the
urgency of fix #1 more precisely).
