"""HTTP request header helpers."""

from __future__ import annotations

from typing import Dict

try:
    from fake_useragent import UserAgent
except ImportError:  # pragma: no cover
    UserAgent = None


def get_default_headers(user_agent: str | None = None) -> Dict[str, str]:
    """Return default headers for HTTP requests."""

    if user_agent:
        ua = user_agent
    elif UserAgent is not None:
        try:
            ua = UserAgent().random
        except Exception:
            ua = "Mozilla/5.0 (compatible; AntiPiracyBot/1.0; +https://example.com/bot)"
    else:
        ua = "Mozilla/5.0 (compatible; AntiPiracyBot/1.0; +https://example.com/bot)"

    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
