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
            ua = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
    else:
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
