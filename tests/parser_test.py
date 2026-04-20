"""Tests for HTML link extractor."""

from parsers.html_link_extractor import HTMLLinkExtractor


def test_html_link_extractor_finds_links():
    html = """
    <html>
      <body>
        <a href="/foo">Foo</a>
        <a href="https://example.com/bar">Bar</a>
        <a href="#fragment">Fragment</a>
      </body>
    </html>
    """

    extractor = HTMLLinkExtractor()
    links = extractor.extract_links(html, "https://example.com")

    assert "https://example.com/foo" in links
    assert "https://example.com/bar" in links
    # The fragment-only link is normalized to the base URL
    assert "https://example.com/" in links


def test_html_link_extractor_drops_irrelevant_external_noise_but_keeps_relevant_targets():
    html = """
    <html>
      <body>
        <a href="/watch/movie-1">Internal watch</a>
        <a href="https://randomblog.example/post">Random blog</a>
        Plain text mirror: https://social.example/profile
        <script>
          const fallback = "https://streamhub.example/watch/123";
        </script>
      </body>
    </html>
    """

    extractor = HTMLLinkExtractor()
    links = extractor.extract_links(html, "https://piracy-site.example")

    assert "https://piracy-site.example/watch/movie-1" in links
    assert "https://streamhub.example/watch/123" in links
    assert "https://randomblog.example/post" not in links
    assert "https://social.example/profile" not in links
