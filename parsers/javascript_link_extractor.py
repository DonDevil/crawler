"""Extract URLs from embedded JavaScript code."""

from __future__ import annotations

import re
from typing import List

from utils.url_utils import URLUtils


class JavaScriptLinkExtractor:
    """Extract URLs found in JavaScript snippets."""

    _url_regex = re.compile(r"https?://[\w\-\.\:/?&=%#]+", re.IGNORECASE)

    def extract_links(self, js: str) -> List[str]:
        """Return normalized URLs found in a JavaScript string."""

        urls: List[str] = []
        for match in self._url_regex.findall(js):
            cleaned = URLUtils.clean_url(match)
            if cleaned:
                urls.append(cleaned)

        return list(dict.fromkeys(urls))
