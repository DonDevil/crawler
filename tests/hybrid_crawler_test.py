"""Tests for hybrid crawler routing and execution."""

import asyncio

import pytest
import redis
from aiohttp import web

from core.network_health import HealthController, NetworkHealthConfig, NetworkHealthState
from core.url_frontier import URLFrontier
from parsers.html_link_extractor import HTMLLinkExtractor
from storage.redis_media_evidence_store import RedisMediaEvidenceStore

_PROBE_ENDPOINTS = ("https://probe-a.example/check", "https://probe-b.example/check")


def _disabled_probe_health(**overrides) -> HealthController:
    """A HealthController whose trigger_threshold is unreachable within a
    single test, so tests can drive state transitions explicitly (by
    setting `._state` directly) without a real/mocked probe ever firing."""
    config = NetworkHealthConfig(
        enabled=True,
        trigger_threshold=1_000_000,
        probe_endpoints=_PROBE_ENDPOINTS,
        **overrides,
    )
    return HealthController(config)


async def _run_test_server():
    app = web.Application()

    async def handler_root(request):
        return web.Response(
            text="<html><body><a href='/page'>page</a></body></html>",
            content_type="text/html",
        )

    async def handler_page(request):
        return web.Response(
            text="<html><body><a href='/'>root</a></body></html>",
            content_type="text/html",
        )

    app.router.add_get("/", handler_root)
    app.router.add_get("/page", handler_page)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = next(iter(site._server.sockets)).getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


def test_router_selects_tor_for_onion_and_async_for_surface():
    from core.crawler_router import CrawlerRouter

    router = CrawlerRouter()

    assert router.select_crawler("http://somedomainhiddenservice.onion/") == "tor"
    assert router.select_crawler("https://example.com/") == "async"


def test_router_prefers_async_then_scrapling_for_surface_urls():
    from core.crawler_router import CrawlerRouter

    router = CrawlerRouter(allow_scrapling=True)

    plan = router.get_engine_plan("https://example.com/watch/movie")

    assert plan[:4] == ["async", "scrapling", "playwright", "selenium"]


def test_router_escalates_antibot_failures_to_scrapling_first():
    from core.crawler_router import CrawlerRouter

    router = CrawlerRouter(allow_scrapling=True)

    plan = router.get_engine_plan(
        "https://example.com/protected",
        current_engine="async",
        failure_reason="not a robot",
    )

    assert plan[:3] == ["scrapling", "playwright", "selenium"]


@pytest.mark.asyncio
async def test_hybrid_crawler_processes_clearweb_pages():
    from crawler.hybrid_crawler import HybridCrawler

    runner, base_url = await _run_test_server()

    try:
        frontier = URLFrontier(rate_limit=0)
        frontier.add_url(base_url)

        crawler = HybridCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=3,
            max_pages=2,
            timeout=5,
            max_retries=1,
        )

        await crawler.run()

        assert base_url in frontier.visited
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_hybrid_crawler_discovers_media_and_submits_exactly_one_fingerprint_job():
    """End-to-end proof of the crawler -> media evidence -> fingerprint job
    path this phase audited (docs/architecture/
    phase-2-crawler-fingerprint-job-trace.md): a page with one media link,
    crawled through the real `HybridCrawler` worker loop and the real HTML
    parser, must produce exactly one queued fingerprint job in a real
    (test-namespaced) Redis media evidence store -- not a stub/mock of
    `record_media_link`, and not the SQLite backend, which is not what
    production is configured to use (config.yaml: media_evidence.type:
    redis)."""
    from crawler.hybrid_crawler import HybridCrawler

    try:
        store = RedisMediaEvidenceStore(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,  # crawler test-isolation DB, never production DB 0
            namespace="test_evidence_e2e",
        )
        store.clear()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")

    async def run_server():
        app = web.Application()

        async def handler_root(request):
            return web.Response(
                text='<html><body><a href="/movie.mp4">download</a></body></html>',
                content_type="text/html",
            )

        app.router.add_get("/", handler_root)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = next(iter(site._server.sockets)).getsockname()[1]
        return runner, f"http://127.0.0.1:{port}/"

    runner, base_url = await run_server()
    try:
        frontier = URLFrontier(rate_limit=0)
        frontier.add_url(base_url)

        crawler = HybridCrawler(
            frontier=frontier,
            parser=HTMLLinkExtractor(),
            concurrency=1,
            max_pages=1,
            timeout=5,
            max_retries=1,
            media_database=store,
        )

        await crawler.run()

        jobs = store.get_fingerprint_jobs()
        assert len(jobs) == 1
        assets = store.list_media_assets()
        assert len(assets) == 1
        assert assets[0]["url"].endswith("/movie.mp4")
    finally:
        await runner.cleanup()
        store.clear()
        store.close()


@pytest.mark.asyncio
async def test_hybrid_crawler_waits_for_inflight_fetches(monkeypatch):
    from crawler import hybrid_crawler as hybrid_module
    from crawler.hybrid_crawler import HybridCrawler

    original_sleep = hybrid_module.asyncio.sleep

    async def fast_idle_sleep(_delay):
        await original_sleep(0)

    async def slow_fetch(self, engine_name, url):
        await original_sleep(0.05)
        return "<html><body>ok</body></html>", None

    monkeypatch.setattr(hybrid_module.asyncio, "sleep", fast_idle_sleep)
    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", slow_fetch)

    frontier = URLFrontier(rate_limit=0)
    frontier.add_url("https://example.com/slow")

    crawler = HybridCrawler(
        frontier=frontier,
        parser=HTMLLinkExtractor(),
        concurrency=1,
        max_pages=1,
        timeout=5,
        max_retries=1,
    )

    await crawler.run()

    assert crawler._pages_crawled == 1
    assert "https://example.com/slow" in frontier.visited


@pytest.mark.asyncio
async def test_hybrid_crawler_marks_failed_once_after_exhausting_engine_escalation(monkeypatch):
    from crawler.hybrid_crawler import HybridCrawler

    async def always_fails(self, engine_name, url):
        return None, "boom"

    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", always_fails)

    frontier = URLFrontier(rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0)
    frontier.add_url("https://example.com/unreachable")

    crawler = HybridCrawler(
        frontier=frontier,
        parser=HTMLLinkExtractor(),
        concurrency=1,
        max_pages=5,
        timeout=5,
        max_retries=1,
    )

    await crawler.run()

    assert "https://example.com/unreachable" not in frontier.visited
    counts = frontier.get_status_counts()
    assert counts["inflight"] == 0
    assert counts["failed_permanent"] == 1


@pytest.mark.asyncio
async def test_hybrid_crawler_heartbeat_spans_full_engine_escalation_chain(monkeypatch):
    """A single claim can span several engine attempts (async -> scrapling
    -> ...) before a final outcome is known -- the heartbeat must wrap the
    whole escalation chain, not just the first engine's fetch, and stop
    once the chain resolves (docs/architecture/frontier-adr.md §11)."""
    from crawler.hybrid_crawler import HybridCrawler

    call_log: list[str] = []

    async def escalating_fetch(self, engine_name, url):
        call_log.append(engine_name)
        await asyncio.sleep(0.12)
        if engine_name == "async":
            return None, "not a robot"  # forces escalation to scrapling/playwright/...
        return "<html><body>ok</body></html>", None

    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", escalating_fetch)

    frontier = URLFrontier(rate_limit=0, lease_ttl=0.1, max_retries=1, base_backoff=0, max_backoff=0)
    frontier.add_url("https://example.com/escalates")

    renew_calls = []
    original_renew = frontier.renew_claim

    def spy(claim):
        renew_calls.append(claim)
        return original_renew(claim)

    frontier.renew_claim = spy

    crawler = HybridCrawler(
        frontier=frontier,
        parser=HTMLLinkExtractor(),
        concurrency=1,
        max_pages=1,
        timeout=5,
        max_retries=1,
        scrapling_enabled=True,
    )
    assert crawler.heartbeat_interval < 0.1

    await crawler.run()

    assert "https://example.com/escalates" in frontier.visited
    assert len(call_log) >= 2, "expected escalation across more than one engine"
    assert len(renew_calls) >= 1, (
        "heartbeat should have renewed at least once across the whole escalation chain"
    )

    calls_at_finish = len(renew_calls)
    await asyncio.sleep(0.3)
    assert len(renew_calls) == calls_at_finish, "heartbeat kept renewing after the chain resolved"


# ---------------------------------------------------------------------
# Network-health integration (N3, docs/architecture/network-failure-handling-design.md)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthy_target_failure_still_uses_normal_mark_failed(monkeypatch):
    """N2 §5/§16: ordinary target failures keep today's semantics
    unchanged, even with a HealthController wired in, as long as it never
    reaches OFFLINE."""
    from crawler.hybrid_crawler import HybridCrawler

    async def always_fails(self, engine_name, url):
        return None, "Connection refused"

    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", always_fails)

    frontier = URLFrontier(rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0)
    frontier.add_url("https://example.com/dead-target")

    health = _disabled_probe_health()
    crawler = HybridCrawler(
        frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
        max_pages=5, timeout=5, max_retries=1, health=health,
    )

    await crawler.run()

    assert health.state == NetworkHealthState.HEALTHY
    counts = frontier.get_status_counts()
    assert counts["failed_permanent"] == 1
    assert counts["retry_scheduled"] == 0


@pytest.mark.asyncio
async def test_offline_at_completion_uses_mark_deferred_not_mark_failed(monkeypatch):
    """N2 §6: a claim taken while HEALTHY that fails after the network is
    confirmed OFFLINE must complete via mark_deferred -- zero
    failed_permanent, retry budget fully preserved (net-zero attempt
    consumption)."""
    from crawler.hybrid_crawler import HybridCrawler

    async def always_fails(self, engine_name, url):
        return None, "Connection refused"

    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", always_fails)

    frontier = URLFrontier(rate_limit=0, max_retries=1, base_backoff=0, max_backoff=0)
    url = "https://example.com/mid-fetch-outage"
    frontier.add_url(url)

    health = _disabled_probe_health()
    crawler = HybridCrawler(
        frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
        max_pages=1, timeout=5, max_retries=1, health=health,
    )

    # Claim directly (bypassing the scheduler, which would otherwise pause
    # claiming once OFFLINE) to reproduce exactly the scenario N2 §6
    # requires: claimed while HEALTHY, network confirmed OFFLINE before
    # this claim's completion.
    claim = frontier.get_next_url()
    assert claim is not None
    crawler.queue.put_nowait(claim)
    health._state = NetworkHealthState.OFFLINE  # probe-confirmed mid-fetch

    await crawler.worker()  # single-shot: max_pages=1 stops it after one claim

    counts = frontier.get_status_counts()
    assert counts["failed_permanent"] == 0, "must never reach failed_permanent while confirmed OFFLINE"
    assert counts["retry_scheduled"] == 1, "must be deferred/requeued, not dropped"
    assert frontier._attempts.get(url, 0) == 0, "retry budget must be net-zero after claim -> defer"


@pytest.mark.asyncio
async def test_engine_escalation_short_circuits_once_offline_confirmed(monkeypatch):
    """N2 §7: once OFFLINE is confirmed, remaining engine-escalation
    attempts (Playwright/Selenium/etc.) are skipped -- no point trying
    another engine with no route to the Internet at all."""
    from crawler.hybrid_crawler import HybridCrawler

    call_log: list[str] = []

    async def always_fails(self, engine_name, url):
        call_log.append(engine_name)
        return None, "Connection refused"

    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", always_fails)

    frontier = URLFrontier(rate_limit=0)
    health = _disabled_probe_health()
    health._state = NetworkHealthState.OFFLINE

    crawler = HybridCrawler(
        frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
        timeout=5, max_retries=1, health=health, scrapling_enabled=True,
    )

    html, failure_reason, attempt_chain, engine_used = await crawler._run_engine_plan(
        "https://example.com/offline-target"
    )

    assert html is None
    assert len(call_log) == 1, f"expected escalation to stop after one engine, tried: {call_log}"
    assert len(attempt_chain) == 1


@pytest.mark.asyncio
async def test_engine_escalation_continues_normally_while_healthy(monkeypatch):
    """Control for the short-circuit test above: with no HealthController
    (or one that's HEALTHY), escalation proceeds through multiple engines
    exactly as before N3."""
    from crawler.hybrid_crawler import HybridCrawler

    call_log: list[str] = []

    async def always_fails(self, engine_name, url):
        call_log.append(engine_name)
        return None, "not a robot"  # forces escalation

    monkeypatch.setattr(HybridCrawler, "_fetch_with_engine", always_fails)

    frontier = URLFrontier(rate_limit=0)
    crawler = HybridCrawler(
        frontier=frontier, parser=HTMLLinkExtractor(), concurrency=1,
        timeout=5, max_retries=1, scrapling_enabled=True,
    )

    html, failure_reason, attempt_chain, engine_used = await crawler._run_engine_plan(
        "https://example.com/normal-target"
    )

    assert html is None
    assert len(call_log) > 1, "expected escalation across multiple engines"


@pytest.mark.asyncio
async def test_scheduler_pauses_claiming_only_for_its_own_offline_controller():
    """Multi-host isolation (N2 §8): host A's confirmed-OFFLINE
    HealthController must pause only A's own claiming; an independent host
    B with its own HEALTHY HealthController must keep claiming normally,
    completely unaffected by A's state."""
    from crawler.hybrid_crawler import HybridCrawler

    frontier_a = URLFrontier(rate_limit=0)
    frontier_a.add_url("https://host-a.example/page")
    frontier_b = URLFrontier(rate_limit=0)
    frontier_b.add_url("https://host-b.example/page")

    health_a = _disabled_probe_health()
    health_a._state = NetworkHealthState.OFFLINE
    health_b = _disabled_probe_health()  # stays HEALTHY

    crawler_a = HybridCrawler(
        frontier=frontier_a, parser=HTMLLinkExtractor(), concurrency=1,
        max_pages=1, timeout=5, max_retries=1, health=health_a,
    )
    crawler_b = HybridCrawler(
        frontier=frontier_b, parser=HTMLLinkExtractor(), concurrency=1,
        max_pages=1, timeout=5, max_retries=1, health=health_b,
    )

    task_a = asyncio.create_task(crawler_a.scheduler())
    task_b = asyncio.create_task(crawler_b.scheduler())
    try:
        await asyncio.sleep(0.3)
    finally:
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

    assert crawler_a.queue.empty(), "host A must not claim while its own health is OFFLINE"
    assert not crawler_b.queue.empty(), "host B must keep claiming -- unaffected by A's state"
    assert health_a.state == NetworkHealthState.OFFLINE
    assert health_b.state == NetworkHealthState.HEALTHY
