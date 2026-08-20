"""Process-local network-health detection (N2, Phase N3).

Implements the `HealthController` state machine and `ConnectivityProber`
from docs/architecture/network-failure-handling-design.md §3/§4. See that
document for the full reasoning; this module implements it, it does not
re-derive it.

Scope reminder (N2 §8/§14): a `HealthController` instance is per-process,
in-memory only, and is never written to Redis or any other shared store.
Two independent `CrawlerManager` processes each hold their own instance and
never observe each other's state -- that isolation is what makes the
multi-host requirement (N1 §8, N2 §8) hold.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional, Sequence

import httpx
from loguru import logger


class NetworkHealthState(str, Enum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    OFFLINE = "offline"


@dataclass(frozen=True)
class NetworkHealthConfig:
    """Mirrors the `network_health.*` config block (N2 §11,
    core/config.py's `NetworkHealthConfig` pydantic model) as a plain
    dataclass -- kept independent of pydantic so this module has no
    dependency on the config-loading layer (N2 §17 item 1: "pure/isolated
    component; no dependency on the frontier or crawler engines")."""

    enabled: bool = True
    trigger_threshold: int = 10
    probe_timeout_seconds: float = 5.0
    probe_endpoints: Sequence[str] = field(default_factory=tuple)
    confirm_delay_seconds: float = 5.0
    recovery_probe_interval_seconds: float = 15.0
    recovery_confirm_rounds: int = 2
    deferred_requeue_delay_seconds: float = 10.0


class ConnectivityProber:
    """Runs one probe round against configured endpoints (N2 §4).

    A probe round succeeds iff *any* endpoint returns *any* HTTP response
    (any status code) within `timeout_seconds`; it fails only when *every*
    endpoint fails at the connection level (DNS, connect, timeout, TLS).
    Redirects are never followed. No response body is read.
    """

    def __init__(
        self,
        endpoints: Sequence[str],
        timeout_seconds: float,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        endpoints = tuple(endpoints)
        if len(endpoints) < 2:
            raise ValueError(
                "network_health requires at least two probe_endpoints (N2 §4); "
                f"got {len(endpoints)}"
            )
        self._endpoints = endpoints
        self._timeout = timeout_seconds
        # Test-only hook: httpx.MockTransport, so unit tests never make a
        # real network call (N2 §17 Phase 9 requirement). `None` uses
        # httpx's real default transport in production.
        self._transport = transport

    async def probe_round(self) -> bool:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=self._timeout, transport=self._transport
        ) as client:
            results = await asyncio.gather(*(self._probe_one(client, e) for e in self._endpoints))
        return any(results)

    async def _probe_one(self, client: httpx.AsyncClient, endpoint: str) -> bool:
        try:
            # HEAD, not GET: any status counts as success, so there is
            # never a reason to read a body (N2 §4, §12 overhead goal).
            await client.head(endpoint)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"network_health probe failed for {endpoint}: {exc}")
            return False


def _default_host_identity() -> str:
    """Process/hostname identifier for observability (N2 §10) -- coarse
    enough to distinguish Host A from Host B without leaking anything more
    sensitive than hostname + pid."""
    return f"{socket.gethostname()}:{os.getpid()}"


class HealthController:
    """Per-process network-health state machine (N2 §3).

    HEALTHY -> SUSPECT -> OFFLINE -> HEALTHY, exactly as specified in N2
    §3/§13. Every transition other than the initial `HEALTHY` startup state
    is driven either by `record_ambiguous_failure`/`record_success` (called
    by the crawler on every completion) or by this class's own background
    probe/recovery tasks -- callers never drive probing directly.

    Concurrency note (N2 §12 INFERENCE): `HybridCrawler` runs its workers
    as cooperatively-scheduled asyncio tasks on one event loop, not OS
    threads, so the counter/state mutations here need no lock -- there is
    no `await` between a read and a write of `_ambiguous_failures` or
    `_state`, so cooperative scheduling serializes access by construction.
    """

    def __init__(
        self,
        config: NetworkHealthConfig,
        prober: Optional[ConnectivityProber] = None,
        host_identity: Optional[str] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ):
        self._config = config
        self._state = NetworkHealthState.HEALTHY
        self._ambiguous_failures = 0
        self.host_identity = host_identity or _default_host_identity()
        self._sleep = sleep or asyncio.sleep

        self._prober: Optional[ConnectivityProber] = None
        if config.enabled:
            self._prober = prober or ConnectivityProber(config.probe_endpoints, config.probe_timeout_seconds)

        self._probe_task: Optional[asyncio.Task] = None
        self._recovery_task: Optional[asyncio.Task] = None

    @property
    def state(self) -> NetworkHealthState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def record_success(self) -> None:
        """Any successful fetch resets the ambiguous-failure counter (N2 §3).

        Deliberately does not by itself move `SUSPECT`/`OFFLINE` back to
        `HEALTHY` -- only a confirmed probe round does that (N2 §3's
        anti-flapping property); a lone success alongside an ongoing outage
        (e.g. a cached/offline-capable response) must not short-circuit
        detection.
        """
        if not self._config.enabled:
            return
        self._ambiguous_failures = 0

    async def record_ambiguous_failure(self) -> None:
        """Feed one category-2/3/5 failure into the trigger counter (N2 §3/§5).

        Non-blocking: crossing `trigger_threshold` schedules the probe
        sequence as a background task and returns immediately -- the
        calling worker never waits on probe timing (N2 §7: only a
        completion's *own* `state` read at completion time matters, not
        whether a probe happens to be in flight).
        """
        if not self._config.enabled or self._state != NetworkHealthState.HEALTHY:
            return

        self._ambiguous_failures += 1
        if self._ambiguous_failures >= self._config.trigger_threshold:
            self._ambiguous_failures = 0
            self._enter_suspect()

    def _enter_suspect(self) -> None:
        if self._state != NetworkHealthState.HEALTHY:
            return
        self._state = NetworkHealthState.SUSPECT
        logger.warning(
            "network_health[{}]: HEALTHY -> SUSPECT (ambiguous-failure threshold reached); "
            "dispatching confirmation probe",
            self.host_identity,
        )
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._run_suspect_confirmation())

    async def _run_suspect_confirmation(self) -> None:
        """N2 §3 SUSPECT: one immediate probe round; if it fails, one more
        after `confirm_delay_seconds`. Any success resolves back to
        HEALTHY; two consecutive failures confirm OFFLINE."""
        try:
            if await self._prober.probe_round():
                self._resolve_to_healthy("initial confirmation probe succeeded")
                return

            await self._sleep(self._config.confirm_delay_seconds)
            if self._state != NetworkHealthState.SUSPECT:
                return  # a concurrent path already resolved this

            if await self._prober.probe_round():
                self._resolve_to_healthy("debounced confirmation probe succeeded")
                return

            self._enter_offline()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fail open: a bug in the prober must never itself declare (or
            # keep declaring) an outage. Log loudly and stay/return HEALTHY.
            logger.error(f"network_health[{self.host_identity}]: probe round raised unexpectedly: {exc}")
            self._resolve_to_healthy("probe error (fail-open)")

    def _resolve_to_healthy(self, reason: str) -> None:
        if self._state != NetworkHealthState.HEALTHY:
            logger.info(f"network_health[{self.host_identity}]: {self._state.value} -> HEALTHY ({reason})")
        self._state = NetworkHealthState.HEALTHY
        self._ambiguous_failures = 0

    def _enter_offline(self) -> None:
        self._state = NetworkHealthState.OFFLINE
        logger.error(
            "network_health[{}]: SUSPECT -> OFFLINE (two consecutive probe rounds failed); "
            "pausing new claims, deferring in-flight failures",
            self.host_identity,
        )
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._run_recovery_loop())

    async def _run_recovery_loop(self) -> None:
        """N2 §3 OFFLINE self-loop: the only periodic (not failure-triggered)
        probing in this design, because while OFFLINE every fetch is
        failing, so there is no other signal to piggyback recovery
        detection on."""
        consecutive_successes = 0
        try:
            while self._state == NetworkHealthState.OFFLINE:
                await self._sleep(self._config.recovery_probe_interval_seconds)
                if self._state != NetworkHealthState.OFFLINE:
                    return

                try:
                    ok = await self._prober.probe_round()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"network_health[{self.host_identity}]: recovery probe raised unexpectedly: {exc}")
                    ok = False

                if ok:
                    consecutive_successes += 1
                    if consecutive_successes >= self._config.recovery_confirm_rounds:
                        self._resolve_to_healthy(
                            f"{consecutive_successes} consecutive recovery probes succeeded"
                        )
                        return
                else:
                    consecutive_successes = 0
        except asyncio.CancelledError:
            raise

    async def wait_idle(self) -> None:
        """Block until the in-flight SUSPECT-confirmation probe task, if
        any, resolves (to HEALTHY or OFFLINE).

        Test-only helper (production call sites must never block on probe
        timing -- see `record_ambiguous_failure`'s docstring). Exists so
        unit tests can deterministically await a `HEALTHY`/`SUSPECT`
        resolution instead of racing a background task. Deliberately does
        NOT also chase into a freshly started `_recovery_task`: the OFFLINE
        recovery loop is intentionally unbounded (it runs until real
        recovery), so a generic "wait for everything" helper would hang
        forever for any test that confirms OFFLINE without also scripting
        an eventual recovery. Use `wait_recovery()` when a test needs to
        observe the OFFLINE -> HEALTHY transition specifically.
        """
        if self._probe_task is not None and not self._probe_task.done():
            await self._probe_task

    async def wait_recovery(self) -> None:
        """Block until the in-flight OFFLINE recovery-loop task, if any,
        resolves back to HEALTHY.

        Test-only helper, like `wait_idle()`. Only returns once the
        injected prober/mock eventually reports enough consecutive
        successes -- a test that calls this must script a recovery, or it
        will hang.
        """
        if self._recovery_task is not None and not self._recovery_task.done():
            await self._recovery_task

    async def aclose(self) -> None:
        """Cancel any in-flight background probe/recovery task. Call this
        on crawler shutdown so no orphaned task outlives the process's
        crawl run."""
        for task in (self._probe_task, self._recovery_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
