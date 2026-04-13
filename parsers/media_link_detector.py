"""Detect media links within HTML pages."""

from __future__ import annotations

from typing import List

from bs4 import BeautifulSoup

from utils.url_utils import URLUtils


class MediaLinkDetector:
    """Extract potential media URLs from an HTML page."""

    def extract_media_links(self, html: str, base_url: str) -> List[str]:
        """Return a list of potentially interesting media URLs."""

        soup = BeautifulSoup(html, "lxml")
        links: List[str] = []

        # Look for <a> tags with media extension
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            full_url = URLUtils.clean_url(href if href.startswith("http") else base_url + href)
            if full_url and URLUtils.is_media_file(full_url):
                links.append(full_url)

        # Look for <video> and <audio> sources
        for tag in soup.find_all(["video", "audio"]):
            src = tag.get("src")
            if src:
                full_url = URLUtils.clean_url(src if src.startswith("http") else base_url + src)
                if full_url:
                    links.append(full_url)

            for source in tag.find_all("source"):
                src = source.get("src")
                if src:
                    full_url = URLUtils.clean_url(src if src.startswith("http") else base_url + src)
                    if full_url:
                        links.append(full_url)

        return list(dict.fromkeys(links))
