"""Failure classification for network-health detection (N2 §5, Phase N3).

See docs/architecture/network-failure-handling-design.md §5 for the full
8-category taxonomy and reasoning. This module implements a pure function
from a fetch failure (a `failure_reason` string and/or the raw exception, if
available) to one of those 8 categories.

Boundary note (discovered during N3 implementation, not a contradiction of
the N2 architecture): every crawler engine's `fetch()` (crawler/*.py)
already collapses its exception into `str(exc)` before returning
`failure_reason` to `HybridCrawler` -- the raw exception object never
reaches the classifier through that call path today. N2 §5 anticipated
exactly this ("prefer structured exception info where available... Unknown
failures must remain conservative") -- `classify_failure` accepts an
optional `exception` for callers that do have one (and unit tests), and
falls back to string matching otherwise, which is what `hybrid_crawler.py`
actually exercises. A failure whose string carries no recognizable
signature (this is a real limitation for a bare `asyncio.TimeoutError`,
whose `str()` is empty, causing the calling engine to substitute the
generic "unknown fetch error" text) classifies as UNKNOWN -- the safe,
conservative default N2 §5 already specifies for exactly this situation.

Category 4 (LOCAL_NETWORK) is deliberately never returned by this module:
per N2 §5/§6, that tag only applies at claim-completion time, when
`HealthController.state == OFFLINE` is independently confirmed -- it is not
something inferable from the failure text/exception alone. Callers apply it
themselves (see `crawler/hybrid_crawler.py`).
"""

from __future__ import annotations

import re
import socket
import ssl
from enum import Enum
from typing import Optional

import aiohttp
import httpx


class FailureCategory(Enum):
    HTTP_RESPONSE = 1
    TARGET_CONNECTION = 2
    TARGET_DNS = 3
    LOCAL_NETWORK = 4
    TIMEOUT = 5
    TLS_FAILURE = 6
    ENGINE_FAILURE = 7
    UNKNOWN = 8


# Categories 2/3/5 (N2 §5): may feed HealthController's ambiguous-failure
# counter. Never themselves grant retry-budget exemption -- only a
# probe-confirmed HealthController.state == OFFLINE at completion time does
# (core/network_health.py). This is the anti-abuse property N2 §5/§14 is
# built around: classification alone can never exempt a URL from its retry
# budget.
AMBIGUOUS_CATEGORIES = frozenset({
    FailureCategory.TARGET_CONNECTION,
    FailureCategory.TARGET_DNS,
    FailureCategory.TIMEOUT,
})


def is_ambiguous(category: FailureCategory) -> bool:
    """True for categories 2/3/5 -- the only categories that feed the
    HealthController's trigger counter (N2 §3/§5)."""
    return category in AMBIGUOUS_CATEGORIES


# ---------------------------------------------------------------------
# String-based classification -- the path actually exercised by
# `crawler/hybrid_crawler.py` today (see module docstring). Checked in a
# fixed priority order: an HTTP-response/antibot signature always wins
# (the target clearly answered), then TLS, DNS, timeout, connection-level,
# then engine/infra signatures. This ordering matters for browser-engine
# failure strings (e.g. Selenium's "WebDriver error: ... net::ERR_..."),
# which must classify by the embedded network signature, not the
# engine-error wrapper, whenever one is present.
# ---------------------------------------------------------------------

_HTTP_RESPONSE_PATTERN = re.compile(r"\bhttp\s+\d{3}\b")

# Mirrors core/crawler_router.py's antibot/JS-required token set: any of
# these mean a response was actually received from the target (that's the
# existing escalation logic's own precondition for treating them as
# target-attributed), so they classify as HTTP_RESPONSE here too --
# required by N2 §17's explicit regression requirement (Cloudflare/
# CAPTCHA/403/429/JS-required must stay target-attributed, not ambiguous).
_HTTP_RESPONSE_SUBSTRINGS = (
    "suspicious redirect",
    "unsupported content type",
    "content requires browser rendering",
    "captcha",
    "cloudflare",
    "just a moment",
    "access denied",
    "browser verification",
    "verify you are human",
    "not a robot",
    "too many requests",
)

# Deliberately does NOT include a bare "ssl"/"tls" substring: aiohttp's
# ClientConnectorError.__str__ unconditionally formats every connector
# error (refused, DNS, timeout -- anything) as
# "Cannot connect to host {host}:{port} ssl:{default|None|True} [{reason}]"
# -- that "ssl:default" token is just echoing the connector's ssl
# parameter, not signaling an actual TLS problem, and a bare "ssl"/"tls"
# substring would misclassify most ordinary aiohttp connection failures as
# TLS_FAILURE. "[ssl:" (with the leading bracket) instead matches Python's
# own `ssl.SSLError` message format ("[SSL: WRONG_VERSION_NUMBER] ..."),
# which aiohttp's boilerplate never produces.
_TLS_SUBSTRINGS = (
    "certificate",
    "cert_",
    "handshake",
    "[ssl:",
    "sslerror",
    "ssl routines",
    "net::err_ssl",
    "net::err_cert",
)

_DNS_SUBSTRINGS = (
    "name or service not known",
    "nodename nor servname",
    "name resolution",
    "getaddrinfo",
    "net::err_name_not_resolved",
    "net::err_name_resolution_failed",
)

_TIMEOUT_SUBSTRINGS = (
    "timeout",
    "timed out",
    "net::err_connection_timed_out",
    "net::err_timed_out",
)

_CONNECTION_SUBSTRINGS = (
    "connection refused",
    "network is unreachable",
    "no route to host",
    "cannot connect to host",
    "connection reset",
    "connection aborted",
    "connection closed",
    "actively refused",
    "net::err_connection_refused",
    "net::err_connection_reset",
    "net::err_connection_closed",
    "net::err_internet_disconnected",
    "net::err_network_changed",
    "net::err_address_unreachable",
    "net::err_proxy_connection_failed",
    "net::err_network_access_denied",
)

_ENGINE_SUBSTRINGS = (
    "webdriver error",
    "browser is not initialized",
    "session unavailable",
    "client unavailable",
    "clients unavailable",
    "fetchers are not installed",
    "is not installed",
    "unsupported engine",
)


def _classify_from_string(reason: str) -> Optional[FailureCategory]:
    lowered = reason.lower()

    if _HTTP_RESPONSE_PATTERN.search(lowered) or any(token in lowered for token in _HTTP_RESPONSE_SUBSTRINGS):
        return FailureCategory.HTTP_RESPONSE

    if any(token in lowered for token in _TLS_SUBSTRINGS):
        return FailureCategory.TLS_FAILURE

    if any(token in lowered for token in _DNS_SUBSTRINGS):
        return FailureCategory.TARGET_DNS

    if any(token in lowered for token in _TIMEOUT_SUBSTRINGS):
        return FailureCategory.TIMEOUT

    if any(token in lowered for token in _CONNECTION_SUBSTRINGS):
        return FailureCategory.TARGET_CONNECTION

    if any(token in lowered for token in _ENGINE_SUBSTRINGS):
        return FailureCategory.ENGINE_FAILURE

    return None


# ---------------------------------------------------------------------
# Exception-type-based classification -- used whenever a caller has an
# actual exception object (not exercised by hybrid_crawler.py today, see
# module docstring; available for direct callers and unit tests). Checked
# before string matching: a structured type is strictly more reliable than
# a message substring.
# ---------------------------------------------------------------------

def _classify_from_exception(exc: BaseException) -> Optional[FailureCategory]:
    # aiohttp: check the specific DNS/SSL subclasses before the generic
    # connector/connection base classes they derive from.
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return FailureCategory.TARGET_DNS
    if isinstance(exc, (aiohttp.ClientConnectorSSLError, aiohttp.ClientSSLError)):
        return FailureCategory.TLS_FAILURE
    if isinstance(exc, aiohttp.ServerTimeoutError):
        return FailureCategory.TIMEOUT
    if isinstance(exc, (aiohttp.ClientConnectorError, aiohttp.ClientConnectionError)):
        return FailureCategory.TARGET_CONNECTION

    # httpx
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return FailureCategory.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        if isinstance(exc.__cause__, socket.gaierror):
            return FailureCategory.TARGET_DNS
        return FailureCategory.TARGET_CONNECTION

    # stdlib
    if isinstance(exc, ssl.SSLError):
        return FailureCategory.TLS_FAILURE
    if isinstance(exc, socket.gaierror):
        return FailureCategory.TARGET_DNS
    if isinstance(exc, TimeoutError):  # covers asyncio.TimeoutError (alias since 3.11)
        return FailureCategory.TIMEOUT
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError)):
        return FailureCategory.TARGET_CONNECTION

    return None


def classify_failure(
    failure_reason: Optional[str] = None,
    exception: Optional[BaseException] = None,
) -> FailureCategory:
    """Classify one fetch failure into the N2 §5 8-category taxonomy.

    Never returns LOCAL_NETWORK (category 4) -- see module docstring.
    Falls back to UNKNOWN (category 8) whenever neither the exception type
    nor the reason string carries a recognizable signature, matching N2
    §5's explicit "unknown stays conservative" requirement.
    """
    if exception is not None:
        category = _classify_from_exception(exception)
        if category is not None:
            return category

    if failure_reason:
        category = _classify_from_string(failure_reason)
        if category is not None:
            return category

    return FailureCategory.UNKNOWN
