'''
parsers/html_link_extractor.py
'''
#----------------------------------------------------------

'''
future development will be switched to selectolax for high speed html extraction
'''
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag
from loguru import logger
from utils.url_utils import URLUtils


class HTMLLinkExtractor:

    def __init__(self, allowed_schemes=None):

        if allowed_schemes is None:
            allowed_schemes = {"http", "https"}

        self.allowed_schemes = allowed_schemes

    def extract_links(self, html, base_url):

        links = set()

        try:

            soup = BeautifulSoup(html, "lxml")

        except Exception as e:

            logger.error(f"HTML parsing failed for {base_url}: {e}")
            return links

        try:

            for tag in soup.find_all("a", href=True):

                href = tag.get("href")

                if not href:
                    continue

                try:

                    absolute_url = urljoin(base_url, href)

                    cleaned = URLUtils.clean_url(absolute_url)
                    
                    if cleaned:
                        links.add(absolute_url)

                except Exception as e:

                    logger.debug(f"Skipping malformed link {href}: {e}")

        except Exception as e:

            logger.error(f"Link extraction error at {base_url}: {e}")

        return links