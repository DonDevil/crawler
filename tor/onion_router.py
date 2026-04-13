"""Utilities for routing requests through Tor."""

from __future__ import annotations

from typing import Optional

from tor.proxy_config import get_default_tor_proxy


class OnionRouter:
    """Provides proxy settings for routing traffic through Tor."""

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy or get_default_tor_proxy()

    def get_proxy(self) -> str:
        return self.proxy
