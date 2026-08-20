"""Tests for the network-health state machine and connectivity prober
(N3, core/network_health.py -- docs/architecture/network-failure-handling-design.md).

No real Internet requests: `ConnectivityProber` is exercised through
`httpx.MockTransport`, and `HealthController` is exercised with injected
zero-delay `sleep`/mocked probers so every test runs instantly and
deterministically.
"""

import asyncio

import httpx
import pytest

from core.network_health import ConnectivityProber, HealthController, NetworkHealthConfig, NetworkHealthState


ENDPOINTS = ("https://endpoint-a.example/check", "https://endpoint-b.example/check")


def _config(**overrides) -> NetworkHealthConfig:
    base = dict(
        enabled=True,
        trigger_threshold=3,
        probe_timeout_seconds=1.0,
        probe_endpoints=ENDPOINTS,
        confirm_delay_seconds=0.0,
        recovery_probe_interval_seconds=0.0,
        recovery_confirm_rounds=2,
        deferred_requeue_delay_seconds=1.0,
    )
    base.update(overrides)
    return NetworkHealthConfig(**base)


async def _noop_sleep(_seconds: float) -> None:
    """Zero-delay stand-in for asyncio.sleep so probe/recovery timing never
    actually waits in tests."""
    await asyncio.sleep(0)


class _ScriptedProber:
    """Test double: returns each element of `results` in order for
    successive `probe_round()` calls (repeats the last value once
    exhausted)."""

    def __init__(self, results: list[bool]):
        self._results = list(results)
        self.calls = 0

    async def probe_round(self) -> bool:
        self.calls += 1
        if not self._results:
            return False
        value = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        return value


def _make_controller(
    prober_results: list[bool], host_identity: str | None = None, **config_overrides
) -> HealthController:
    config = _config(**config_overrides)
    prober = _ScriptedProber(prober_results)
    return HealthController(config, prober=prober, sleep=_noop_sleep, host_identity=host_identity)


# ---------------------------------------------------------------------
# ConnectivityProber
# ---------------------------------------------------------------------

class TestConnectivityProber:
    def test_requires_at_least_two_endpoints(self):
        with pytest.raises(ValueError):
            ConnectivityProber(["https://only-one.example"], timeout_seconds=1.0)

    @pytest.mark.asyncio
    async def test_any_endpoint_success_is_probe_success(self):
        calls = {"a": 0, "b": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "endpoint-a" in str(request.url):
                calls["a"] += 1
                raise httpx.ConnectError("connection refused", request=request)
            calls["b"] += 1
            return httpx.Response(200)

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        assert await prober.probe_round() is True
        assert calls["a"] == 1 and calls["b"] == 1

    @pytest.mark.asyncio
    async def test_all_endpoints_fail_is_probe_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        assert await prober.probe_round() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 204, 404, 500, 503])
    async def test_any_http_status_counts_as_success(self, status):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        assert await prober.probe_round() is True

    @pytest.mark.asyncio
    async def test_timeout_is_probe_endpoint_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        assert await prober.probe_round() is False

    @pytest.mark.asyncio
    async def test_dns_failure_is_probe_endpoint_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        assert await prober.probe_round() is False

    @pytest.mark.asyncio
    async def test_tls_failure_is_probe_endpoint_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED", request=request)

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        assert await prober.probe_round() is False

    @pytest.mark.asyncio
    async def test_redirects_are_not_followed(self):
        seen_paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://attacker.example/steal"})

        prober = ConnectivityProber(ENDPOINTS, timeout_seconds=1.0, transport=httpx.MockTransport(handler))
        result = await prober.probe_round()

        assert result is True  # a 302 is still "a response" -- success
        # Exactly one request per configured endpoint -- the redirect
        # target was never fetched.
        assert len(seen_paths) == len(ENDPOINTS)
        assert all("attacker.example" not in p for p in seen_paths)


# ---------------------------------------------------------------------
# HealthController state machine (N2 §3/§13 scenarios 1-2, 5, 9)
# ---------------------------------------------------------------------

class TestHealthControllerStateMachine:
    def test_starts_healthy(self):
        controller = _make_controller(prober_results=[True])
        assert controller.state == NetworkHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_isolated_ambiguous_failure_stays_healthy_below_threshold(self):
        controller = _make_controller(prober_results=[True], trigger_threshold=3)
        await controller.record_ambiguous_failure()
        assert controller.state == NetworkHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_success_resets_ambiguous_counter_so_interleaved_failures_never_trip(self):
        """N2 §13 scenario 4: successes interleaved between ambiguous
        failures must prevent ever reaching trigger_threshold."""
        controller = _make_controller(prober_results=[True], trigger_threshold=3)
        for _ in range(10):
            await controller.record_ambiguous_failure()
            controller.record_success()
        assert controller.state == NetworkHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_threshold_reached_enters_suspect_then_probe_succeeds_back_to_healthy(self):
        """N2 §13 scenario 5 (first half): trigger_threshold ambiguous
        failures with no interleaved success -> SUSPECT -> probe succeeds
        -> HEALTHY."""
        controller = _make_controller(prober_results=[True], trigger_threshold=3)

        for _ in range(3):
            await controller.record_ambiguous_failure()

        await controller.wait_idle()
        assert controller.state == NetworkHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_first_probe_fails_confirmation_probe_succeeds_stays_healthy(self):
        controller = _make_controller(prober_results=[False, True], trigger_threshold=1)

        await controller.record_ambiguous_failure()
        await controller.wait_idle()

        assert controller.state == NetworkHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_both_probe_rounds_fail_confirms_offline(self):
        """N2 §13 scenario 5 (second half): two consecutive failed probe
        rounds -> OFFLINE."""
        controller = _make_controller(prober_results=[False, False], trigger_threshold=1)

        await controller.record_ambiguous_failure()
        await controller.wait_idle()

        assert controller.state == NetworkHealthState.OFFLINE

    @pytest.mark.asyncio
    async def test_offline_pauses_are_observable_via_state(self):
        """The crawler's own pause behavior is wired at the call site
        (crawler/hybrid_crawler.py); this controller's contract is simply
        that `state == OFFLINE` is externally observable once confirmed."""
        controller = _make_controller(prober_results=[False, False], trigger_threshold=1)
        await controller.record_ambiguous_failure()
        await controller.wait_idle()
        assert controller.state == NetworkHealthState.OFFLINE

    @pytest.mark.asyncio
    async def test_recovery_resets_consecutive_success_count_on_intervening_failure(self):
        """A failed recovery-probe round between two successes must reset
        the consecutive-success count -- two non-consecutive successes must
        not satisfy recovery_confirm_rounds=2."""
        config = _config(trigger_threshold=1, recovery_confirm_rounds=2)
        # calls 1-2: SUSPECT initial + confirmation (both fail) -> OFFLINE.
        # calls 3-6: recovery loop -- True, False (reset!), True, True.
        prober = _ScriptedProber([False, False, True, False, True, True])
        controller = HealthController(config, prober=prober, sleep=_noop_sleep)

        await controller.record_ambiguous_failure()
        await controller.wait_idle()
        assert controller.state == NetworkHealthState.OFFLINE

        await controller.wait_recovery()

        assert controller.state == NetworkHealthState.HEALTHY
        # A buggy implementation that failed to reset the consecutive
        # count on the intervening failure would reach recovery_confirm_
        # rounds (2) one probe round earlier (5 total calls, not 6).
        assert prober.calls == 6

    @pytest.mark.asyncio
    async def test_recovery_confirm_rounds_then_offline_to_healthy(self):
        """N2 §13 scenario 9: recovery_confirm_rounds consecutive successful
        probe rounds while OFFLINE -> HEALTHY."""
        config = _config(trigger_threshold=1, recovery_confirm_rounds=2)
        prober = _ScriptedProber([False, False, True, True])
        controller = HealthController(config, prober=prober, sleep=_noop_sleep)

        await controller.record_ambiguous_failure()
        await controller.wait_idle()
        assert controller.state == NetworkHealthState.OFFLINE

        await controller.wait_recovery()
        assert controller.state == NetworkHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_disabled_controller_never_transitions(self):
        controller = _make_controller(prober_results=[False, False], trigger_threshold=1, enabled=False)
        for _ in range(10):
            await controller.record_ambiguous_failure()
        assert controller.state == NetworkHealthState.HEALTHY


# ---------------------------------------------------------------------
# Multi-host isolation (N2 §8, N3 Phase 9 requirement)
# ---------------------------------------------------------------------

class TestMultiHostIsolation:
    @pytest.mark.asyncio
    async def test_two_independent_controllers_do_not_affect_each_other(self):
        host_a = _make_controller(prober_results=[False, False], trigger_threshold=1)
        host_b = _make_controller(prober_results=[True], trigger_threshold=1)

        for _ in range(1):
            await host_a.record_ambiguous_failure()
        await host_a.wait_idle()

        assert host_a.state == NetworkHealthState.OFFLINE
        assert host_b.state == NetworkHealthState.HEALTHY  # completely untouched

        # host_b independently experiences its own healthy-staying failures
        host_b.record_success()
        assert host_b.state == NetworkHealthState.HEALTHY
        assert host_a.state == NetworkHealthState.OFFLINE  # still untouched by B

    def test_host_identity_differs_by_construction_when_overridden(self):
        host_a = _make_controller(prober_results=[True], host_identity="host-a:123")
        host_b = _make_controller(prober_results=[True], host_identity="host-b:456")
        assert host_a.host_identity != host_b.host_identity
