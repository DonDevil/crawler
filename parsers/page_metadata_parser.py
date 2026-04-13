"""Page metadata extraction utilities."""

from __future__ import annotations

from typing import Dict

from bs4 import BeautifulSoup


class PageMetadataParser:
    """Extract metadata from an HTML page."""

    def extract(self, html: str) -> Dict[str, str]:
        """Return metadata such as title and description."""

        soup = BeautifulSoup(html, "lxml")
        metadata: Dict[str, str] = {}

        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            metadata["title"] = title_tag.string.strip()

        for name in ["description", "og:description", "twitter:description"]:
            tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
            if tag and tag.get("content"):
                metadata[name] = tag["content"].strip()

        for name in ["og:title", "twitter:title"]:
            tag = soup.find("meta", attrs={"property": name})
            if tag and tag.get("content"):
                metadata[name] = tag["content"].strip()

        return metadata
