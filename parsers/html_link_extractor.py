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

    def __init__(self, allowed_schemes=None):

        if allowed_schemes is None:
            allowed_schemes = {"http", "https"}

        self.allowed_schemes = allowed_schemes
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

            for tag in soup.find_all("a", href=True):

                href = tag.get("href")

                if not href:
                    continue

                found_raw += 1

                try:

                    absolute_url = urljoin(base_url, href)

                    cleaned = URLUtils.clean_url(absolute_url)

                    if not cleaned:
                        logger.debug(f"Link FILTERED by clean_url: {absolute_url} (raw: {href})")
                        continue

                    links.add(cleaned)
                    found_valid += 1
                    logger.debug(f"Link ADDED: {cleaned}")

                except Exception as e:

                    logger.debug(f"Link ERROR: {href}: {e}")

            # Also attempt to extract URLs from embedded scripts and raw text.
            script_links = 0
            for script in soup.find_all("script"):
                if script.string:
                    for link in self._js_extractor.extract_links(script.string):
                        cleaned = URLUtils.clean_url(urljoin(base_url, link))
                        if not cleaned or cleaned in links:
                            continue
                        links.add(cleaned)
                        script_links += 1
                        logger.debug(f"Link from script: {cleaned}")

            text_links = 0
            for link in self._js_extractor.extract_links(soup.get_text()):
                cleaned = URLUtils.clean_url(urljoin(base_url, link))
                if not cleaned or cleaned in links:
                    continue
                links.add(cleaned)
                text_links += 1
                logger.debug(f"Link from text: {cleaned}")

            logger.info(f"EXTRACTION RESULT from {base_url}: found {found_raw} <a> tags, {found_valid} valid, {script_links} scripts, {text_links} text = {len(links)} TOTAL unique")

        except Exception as e:

            logger.error(f"Link extraction error at {base_url}: {e}")

        return links
