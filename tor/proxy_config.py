"""Tor proxy configuration helpers."""

from __future__ import annotations


def get_default_tor_proxy() -> str:
    """Return the default Tor SOCKS proxy URL."""
    return "socks5h://127.0.0.1:9050"


def get_httpx_tor_proxies() -> dict[str, str]:
    """Return proxies dict compatible with httpx."""
    proxy = get_default_tor_proxy()
    return {
        "http://": proxy,
        "https://": proxy,
    }
