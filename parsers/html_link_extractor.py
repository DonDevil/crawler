'''
parsers/html_link_extractor.py
'''
#----------------------------------------------------------

'''
future development will be switched to selectolax for high speed html extraction
'''
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from loguru import logger

from parsers.javascript_link_extractor import JavaScriptLinkExtractor
from parsers.media_link_detector import MediaLinkDetector
from utils.url_utils import URLUtils


class HTMLLinkExtractor:

    def __init__(self, allowed_schemes=None, max_external_links_per_page=10):

        if allowed_schemes is None:
            allowed_schemes = {"http", "https"}

        self.allowed_schemes = allowed_schemes
        self.max_external_links_per_page = max(0, int(max_external_links_per_page))
        self._js_extractor = JavaScriptLinkExtractor()
        self._media_detector = MediaLinkDetector()

    def extract_content(self, html, base_url):
        return {
            "links": self.extract_links(html, base_url),
            "media_links": self._media_detector.extract_media_links(html, base_url),
        }

    def extract_links(self, html, base_url):

        links = set()

        try:

            soup = BeautifulSoup(html, "lxml")

        except Exception as e:

            logger.error(f"HTML parsing failed for {base_url}: {e}")
            return links

        try:

            found_raw = 0
            found_valid = 0
            external_links_kept = 0

            def _record_link(candidate_url, *, source_label: str, from_text: bool = False):
                nonlocal found_valid, script_links, text_links, external_links_kept

                cleaned = URLUtils.clean_url(candidate_url)
                if not cleaned:
                    logger.debug(f"Link FILTERED by clean_url: {candidate_url}")
                    return

                if cleaned in links:
                    return

                is_same_domain = URLUtils.same_registered_domain(base_url, cleaned)
                if not URLUtils.should_queue_link(base_url, cleaned, from_text=from_text):
                    logger.debug(f"Link FILTERED by relevance: {cleaned}")
                    return

                if not is_same_domain:
                    if external_links_kept >= self.max_external_links_per_page:
                        logger.debug(f"Link FILTERED by external budget: {cleaned}")
                        return
                    external_links_kept += 1

                links.add(cleaned)
                if source_label == "anchor":
                    found_valid += 1
                elif source_label == "script":
                    script_links += 1
                elif source_label == "text":
                    text_links += 1

                logger.debug(f"Link ADDED from {source_label}: {cleaned}")

            script_links = 0
            text_links = 0

            for tag in soup.find_all("a", href=True):

                href = tag.get("href")

                if not href:
                    continue

                found_raw += 1

                try:
                    absolute_url = urljoin(base_url, href)
                    _record_link(absolute_url, source_label="anchor")
                except Exception as e:
                    logger.debug(f"Link ERROR: {href}: {e}")

            # Also attempt to extract URLs from embedded scripts and raw text.
            for script in soup.find_all("script"):
                if script.string:
                    for link in self._js_extractor.extract_links(script.string):
                        _record_link(urljoin(base_url, link), source_label="script", from_text=True)

            for link in self._js_extractor.extract_links(soup.get_text()):
                _record_link(urljoin(base_url, link), source_label="text", from_text=True)

            logger.info(f"EXTRACTION RESULT from {base_url}: found {found_raw} <a> tags, {found_valid} valid, {script_links} scripts, {text_links} text = {len(links)} TOTAL unique")

        except Exception as e:

            logger.error(f"Link extraction error at {base_url}: {e}")

        return links
