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
