"""Routing heuristics for selecting the most appropriate crawler per URL/page."""

from __future__ import annotations

from urllib.parse import urlparse

from intelligence.piracy_domain_classifier import PiracyDomainClassifier
from utils.url_utils import URLUtils


JS_HEAVY_PATH_TOKENS = {
    "/watch",
    "/stream",
    "/player",
    "/embed",
    "/play",
    "/episode",
    "/movies",
    "/series",
}

JS_REQUIRED_MARKERS = {
    "enable javascript",
    "javascript required",
    "please turn javascript on",
    "cf-browser-verification",
    "just a moment",
    "captcha",
    "data-reactroot",
    "__next_data__",
    "id=\"app\"",
    "id=\"root\"",
    "ng-app",
}


class CrawlerRouter:
    """Choose crawler strategies based on URL traits and fetched content."""

    def __init__(
        self,
        classifier: PiracyDomainClassifier | None = None,
        allow_scrapling: bool = False,
    ):
        self.classifier = classifier or PiracyDomainClassifier()
        self.allow_scrapling = allow_scrapling

    def _clean_or_raw(self, url: str) -> str:
        return URLUtils.clean_url(url) or url

    def _hostname(self, url: str) -> str:
        parsed = urlparse(self._clean_or_raw(url))
        return (parsed.hostname or "").lower()

    def _path(self, url: str) -> str:
        parsed = urlparse(self._clean_or_raw(url))
        return (parsed.path or "/").lower()

    def prefers_browser(self, url: str) -> bool:
        """Return True when URL hints suggest a JS-heavy page."""

        if URLUtils.is_onion_url(url):
            return False

        path = self._path(url)
        hostname = self._hostname(url)

        if any(token in path for token in JS_HEAVY_PATH_TOKENS):
            return True

        if hostname and self.classifier.is_piracy_domain(hostname):
            if any(token in path for token in ("watch", "stream", "play", "download")):
                return True

        return False

    def select_crawler(self, url: str) -> str:
        """Pick the best initial crawler for a URL."""

        if URLUtils.is_onion_url(url):
            return "tor"

        return "async"

    def needs_browser_upgrade(
        self,
        url: str,
        html: str | None = None,
        failure_reason: str | None = None,
    ) -> bool:
        """Decide whether a browser crawler should be used after inspection."""

        if URLUtils.is_onion_url(url):
            return False

        if failure_reason:
            lowered_reason = failure_reason.lower()
            if any(token in lowered_reason for token in (
                "captcha",
                "cloudflare",
                "just a moment",
                "access denied",
                "browser verification",
                "javascript",
                "403",
                "401",
                "429",
                "202",
                "too many requests",
                "verify you are human",
                "not a robot",
            )):
                return True

        if not html:
            return False

        lowered_html = html.lower()
        if any(marker in lowered_html for marker in JS_REQUIRED_MARKERS):
            return True

        script_count = lowered_html.count("<script")
        link_count = lowered_html.count("<a ")
        if script_count >= 6 and link_count <= 1:
            return True

        return False

    def get_engine_plan(
        self,
        url: str,
        current_engine: str | None = None,
        html: str | None = None,
        failure_reason: str | None = None,
    ) -> list[str]:
        """Return an ordered list of engines to try for a URL."""

        if URLUtils.is_onion_url(url):
            return ["tor"]

        if current_engine is None:
            plan = ["async"]
            if self.allow_scrapling:
                plan.append("scrapling")
            plan.extend(["playwright", "selenium", "http"])
            return plan

        if self.needs_browser_upgrade(url, html=html, failure_reason=failure_reason):
            preferred = []
            if self.allow_scrapling:
                preferred.append("scrapling")
            preferred.extend(["playwright", "selenium", "http", "async"])
            return [engine for engine in preferred if engine != current_engine]

        fallback_order = {
            "async": ["scrapling", "playwright", "selenium", "http"],
            "scrapling": ["playwright", "selenium", "http"],
            "playwright": ["selenium", "http", "async"],
            "selenium": ["playwright", "http", "async"],
            "http": ["scrapling", "playwright", "selenium", "async"],
            "tor": [],
        }

        if not self.allow_scrapling:
            fallback_order = {
                name: [engine for engine in plan if engine != "scrapling"]
                for name, plan in fallback_order.items()
            }

        default_plan = ["async", "playwright", "selenium", "http"]
        if self.allow_scrapling:
            default_plan.insert(1, "scrapling")

        return fallback_order.get(current_engine, default_plan)
