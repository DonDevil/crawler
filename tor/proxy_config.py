"""Tor proxy configuration helpers."""

from __future__ import annotations

import os
import socket


DEFAULT_TOR_HOST = "127.0.0.1"
DEFAULT_TOR_PORTS = (9050, 9150)


def _is_local_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _iter_candidate_ports() -> tuple[int, ...]:
    env_port = os.getenv("TOR_SOCKS_PORT")
    if env_port:
        try:
            return (int(env_port),)
        except ValueError:
            pass

    return DEFAULT_TOR_PORTS


def get_default_tor_proxy() -> str:
    """Return the best available Tor SOCKS proxy URL."""

    env_proxy = os.getenv("TOR_SOCKS_PROXY")
    if env_proxy:
        return env_proxy

    for port in _iter_candidate_ports():
        if _is_local_port_open(DEFAULT_TOR_HOST, port):
            return f"socks5h://{DEFAULT_TOR_HOST}:{port}"

    return f"socks5h://{DEFAULT_TOR_HOST}:{DEFAULT_TOR_PORTS[0]}"


def get_httpx_tor_proxies() -> dict[str, str]:
    """Return proxies dict compatible with httpx."""
    proxy = get_default_tor_proxy()
    return {
        "http://": proxy,
        "https://": proxy,
    }
