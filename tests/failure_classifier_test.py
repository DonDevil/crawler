"""Tests for the failure classifier (N3, core/failure_classifier.py --
docs/architecture/network-failure-handling-design.md §5).

One test per category (via string classification, the path
`crawler/hybrid_crawler.py` actually exercises today) plus the explicit N2
§17 regression requirement: antibot/JS-required signatures must stay
target-attributed (HTTP_RESPONSE), never ambiguous.
"""

import socket
import ssl

import httpx
import pytest

from core.failure_classifier import FailureCategory, classify_failure, is_ambiguous


class TestCategoryOne_HttpResponse:
    @pytest.mark.parametrize("reason", ["HTTP 403", "HTTP 404", "HTTP 429", "HTTP 500"])
    def test_http_status_strings(self, reason):
        assert classify_failure(reason) == FailureCategory.HTTP_RESPONSE

    def test_suspicious_redirect(self):
        assert classify_failure("Suspicious redirect to https://evil.example") == FailureCategory.HTTP_RESPONSE

    def test_unsupported_content_type(self):
        assert classify_failure("Unsupported content type: application/zip") == FailureCategory.HTTP_RESPONSE


class TestCategoryTwo_TargetConnection:
    @pytest.mark.parametrize("reason", [
        "Connection refused",
        "[Errno 111] Connection refused",
        "Network is unreachable",
        "Cannot connect to host example.com:443 ssl:default",
        "Connection reset by peer",
        "net::ERR_CONNECTION_REFUSED at https://example.com/",
        "net::ERR_INTERNET_DISCONNECTED",
    ])
    def test_connection_strings(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.TARGET_CONNECTION
        assert is_ambiguous(category)

    def test_connection_refused_exception_type(self):
        category = classify_failure(exception=ConnectionRefusedError("refused"))
        assert category == FailureCategory.TARGET_CONNECTION
        assert is_ambiguous(category)


class TestCategoryThree_TargetDns:
    @pytest.mark.parametrize("reason", [
        "Temporary failure in name resolution",
        "[Errno -2] Name or service not known",
        "nodename nor servname provided, or not known",
        "getaddrinfo failed",
        "net::ERR_NAME_NOT_RESOLVED at https://example.com/",
    ])
    def test_dns_strings(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.TARGET_DNS
        assert is_ambiguous(category)

    def test_gaierror_exception_type(self):
        category = classify_failure(exception=socket.gaierror("Name or service not known"))
        assert category == FailureCategory.TARGET_DNS
        assert is_ambiguous(category)

    def test_httpx_connect_error_wrapping_gaierror(self):
        request = httpx.Request("HEAD", "https://example.com")
        exc = httpx.ConnectError("getaddrinfo failed", request=request)
        exc.__cause__ = socket.gaierror("Name or service not known")
        assert classify_failure(exception=exc) == FailureCategory.TARGET_DNS


class TestCategoryFour_LocalNetwork:
    def test_classifier_never_returns_local_network(self):
        """Category 4 is assigned only at claim-completion time by the
        caller (crawler/hybrid_crawler.py), when HealthController.state ==
        OFFLINE -- it is never inferable from the failure text/exception
        alone (N2 §5)."""
        samples = [
            "HTTP 500", "Connection refused", "Temporary failure in name resolution",
            "Timeout", "SSL: CERTIFICATE_VERIFY_FAILED", "WebDriver error: boom", "", None,
        ]
        for reason in samples:
            assert classify_failure(reason) != FailureCategory.LOCAL_NETWORK


class TestCategoryFive_Timeout:
    @pytest.mark.parametrize("reason", [
        "TimeoutError",
        "Connection timed out",
        "Read timed out",
        "net::ERR_CONNECTION_TIMED_OUT",
    ])
    def test_timeout_strings(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.TIMEOUT
        assert is_ambiguous(category)

    def test_timeout_exception_type(self):
        category = classify_failure(exception=TimeoutError())
        assert category == FailureCategory.TIMEOUT
        assert is_ambiguous(category)

    def test_httpx_connect_timeout_exception_type(self):
        request = httpx.Request("HEAD", "https://example.com")
        exc = httpx.ConnectTimeout("timed out", request=request)
        assert classify_failure(exception=exc) == FailureCategory.TIMEOUT

    def test_bare_timeout_error_with_empty_str_is_unknown_not_misclassified(self):
        """Known, accepted limitation (see module docstring): every crawler
        engine converts its exception to `str(exc)` before HybridCrawler
        ever sees it, and substitutes "unknown fetch error" whenever that
        string is empty (as it is for a bare `TimeoutError()`). By the time
        the classifier sees it, there is no timeout signature left in the
        string -- it must fall back to UNKNOWN, the safe/conservative
        default N2 §5 specifies for exactly this situation, not silently
        guess TIMEOUT."""
        assert classify_failure("unknown fetch error") == FailureCategory.UNKNOWN


class TestCategorySix_TlsFailure:
    @pytest.mark.parametrize("reason", [
        "SSL: CERTIFICATE_VERIFY_FAILED",
        "[SSL: WRONG_VERSION_NUMBER] wrong version number",
        "certificate verify failed",
        "TLS handshake failed",
        "net::ERR_CERT_AUTHORITY_INVALID",
    ])
    def test_tls_strings(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.TLS_FAILURE
        assert not is_ambiguous(category)

    def test_ssl_error_exception_type(self):
        category = classify_failure(exception=ssl.SSLError("cert error"))
        assert category == FailureCategory.TLS_FAILURE
        assert not is_ambiguous(category)


class TestCategorySeven_EngineFailure:
    @pytest.mark.parametrize("reason", [
        "WebDriver error: chrome not reachable",
        "Playwright browser is not initialized",
        "Direct session unavailable",
        "Tor clients unavailable",
        "Scrapling fetchers are not installed",
    ])
    def test_engine_strings(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.ENGINE_FAILURE
        assert not is_ambiguous(category)

    def test_network_signature_inside_webdriver_wrapper_still_wins(self):
        """A browser-engine navigation failure that itself carries a
        network signature (Chromium's net::ERR_* codes) must classify by
        that embedded signature, not the generic 'WebDriver error:'
        wrapper -- Playwright/Selenium surface real connectivity failures
        this way too."""
        category = classify_failure("WebDriver error: unknown error: net::ERR_INTERNET_DISCONNECTED")
        assert category == FailureCategory.TARGET_CONNECTION
        assert is_ambiguous(category)


class TestCategoryEight_Unknown:
    @pytest.mark.parametrize("reason", [None, "", "something completely unrecognized happened"])
    def test_unknown_fallback(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.UNKNOWN
        assert not is_ambiguous(category)


class TestAntibotRegression:
    """N2 §17 explicit regression requirement: Cloudflare/CAPTCHA/403/429/
    JS-required must classify as target-attributed (HTTP_RESPONSE), never
    ambiguous -- existing browser-escalation behavior
    (core/crawler_router.py's `needs_browser_upgrade`) must not regress."""

    @pytest.mark.parametrize("reason", [
        "not a robot",
        "cloudflare challenge detected",
        "Just a moment...",
        "captcha required",
        "Access Denied",
        "browser verification needed",
        "verify you are human",
        "HTTP 403",
        "HTTP 429",
        "too many requests",
    ])
    def test_stays_target_attributed(self, reason):
        category = classify_failure(reason)
        assert category == FailureCategory.HTTP_RESPONSE
        assert not is_ambiguous(category)
