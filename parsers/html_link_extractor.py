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
from utils.url_utils import URLUtils


class HTMLLinkExtractor:

    def __init__(self, allowed_schemes=None):

        if allowed_schemes is None:
            allowed_schemes = {"http", "https"}

        self.allowed_schemes = allowed_schemes
        self._js_extractor = JavaScriptLinkExtractor()

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
                        if link not in links:
                            links.add(link)
                            script_links += 1
                            logger.debug(f"Link from script: {link}")

            text_links = 0
            for link in self._js_extractor.extract_links(soup.get_text()):
                if link not in links:
                    links.add(link)
                    text_links += 1
                    logger.debug(f"Link from text: {link}")

            logger.info(f"EXTRACTION RESULT from {base_url}: found {found_raw} <a> tags, {found_valid} valid, {script_links} scripts, {text_links} text = {len(links)} TOTAL unique")

        except Exception as e:

            logger.error(f"Link extraction error at {base_url}: {e}")

        return links
