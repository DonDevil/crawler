"""Detect media links within HTML pages."""

from __future__ import annotations

from typing import List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.javascript_link_extractor import JavaScriptLinkExtractor
from utils.url_utils import URLUtils


class MediaLinkDetector:
    """Extract potential media URLs from an HTML page."""

    def __init__(self) -> None:
        self._js_extractor = JavaScriptLinkExtractor()

    def extract_media_links(self, html: str, base_url: str) -> List[dict]:
        """Return structured media-link candidates for evidence storage."""

        soup = BeautifulSoup(html, "lxml")
        links: dict[str, dict] = {}

        def add_candidate(raw_url: str | None, detection_method: str, mime_type: str | None = None) -> None:
            if not raw_url:
                return

            full_url = URLUtils.clean_media_url(urljoin(base_url, raw_url.strip()))
            if not full_url:
                return

            media_type = URLUtils.classify_media_url(full_url, mime_type)
            if media_type == "unknown":
                return

            links.setdefault(
                full_url,
                {
                    "url": full_url,
                    "media_type": media_type,
                    "mime_type": mime_type,
                    "detection_method": detection_method,
                },
            )

        for tag in soup.find_all("a", href=True):
            add_candidate(tag.get("href"), "anchor")

        for tag in soup.find_all(["video", "audio", "iframe", "embed"]):
            add_candidate(tag.get("src"), f"{tag.name}-tag")

            for source in tag.find_all("source"):
                add_candidate(source.get("src"), "source-tag", source.get("type"))

        for script in soup.find_all("script"):
            script_text = script.string or script.get_text() or ""
            for candidate in self._js_extractor.extract_links(script_text):
                add_candidate(candidate, "script-url")

        for candidate in self._js_extractor.extract_links(soup.get_text(" ")):
            add_candidate(candidate, "text-url")

        return list(links.values())
