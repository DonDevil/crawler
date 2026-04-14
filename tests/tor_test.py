"""Tests for Tor proxy configuration and Torch search scraping."""

from bs4 import BeautifulSoup

from search_engines.torch_search import TorchSearch
from tor import proxy_config


def test_get_default_tor_proxy_prefers_open_port(monkeypatch):
    monkeypatch.delenv("TOR_SOCKS_PROXY", raising=False)
    monkeypatch.delenv("TOR_SOCKS_PORT", raising=False)
    monkeypatch.setattr(proxy_config, "_is_local_port_open", lambda host, port, timeout=0.5: port == 9150)

    assert proxy_config.get_default_tor_proxy() == "socks5h://127.0.0.1:9150"


def test_torch_search_uses_working_mirror(monkeypatch):
    engine = TorchSearch(proxy="socks5h://127.0.0.1:9050")
    calls = []

    def fake_make_soup(url, *, params=None, proxy=None):
        calls.append((url, params, proxy))
        html = """
        <html><body>
            <a href="advertise.htm">ad</a>
            <a href="http://examplehiddenservice.onion/">result</a>
        </body></html>
        """
        return BeautifulSoup(html, "lxml"), url

    monkeypatch.setattr(engine, "_make_soup", fake_make_soup)

    assert engine.search("movie", max_results=5) == ["http://examplehiddenservice.onion/"]
    assert calls[0][1] == {"P": "movie", "DEFAULTOP": "and"}