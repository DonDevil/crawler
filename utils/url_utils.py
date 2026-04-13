'''
utils/url_utils.py
'''
#----------------------------------------------------------
'''
Use Case : 
URL normalization
URL validation
duplicate reduction
domain extraction
query filtering
crawler trap detection

Design Goals

The module should:
Normalize URLs
Remove fragments
Remove tracking parameters
Validate schemes
Extract domains
Detect crawler traps
Filter unwanted file types
'''
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urldefrag
import re


TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}


UNWANTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".mp4",
    ".mp3",
    ".avi",
    ".mkv",
    ".pdf",
    ".zip",
    ".rar",
    ".tar",
    ".gz",
}


class URLUtils:

    @staticmethod
    def normalize_url(url):

        try:
            url, _ = urldefrag(url)

            parsed = urlparse(url)

            # Handle missing scheme/netloc (e.g. "example.com/path" or "www.example.com").
            if not parsed.scheme or not parsed.netloc:
                if parsed.path:
                    url = "http://" + url.lstrip("/")
                    parsed = urlparse(url)

            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()
            path = parsed.path or "/"

            query_params = parse_qsl(parsed.query)
            filtered_query = []

            for key, value in query_params:
                if key not in TRACKING_PARAMETERS:
                    filtered_query.append((key, value))

            query = urlencode(filtered_query)
            normalized = urlunparse((scheme, netloc, path, "", query, ""))

            return normalized

        except Exception:
            return None

    @staticmethod
    def is_valid_scheme(url):

        parsed = urlparse(url)

        return parsed.scheme in ("http", "https")

    @staticmethod
    def extract_domain(url):

        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except Exception:
            return None

    @staticmethod
    def is_media_file(url):

        url = url.lower()

        for ext in UNWANTED_EXTENSIONS:
            if url.endswith(ext):
                return True

        return False

    @staticmethod
    def is_probable_trap(url):

        parsed = urlparse(url)

        path = parsed.path

        if re.search(r"\d{4}/\d{2}/\d{2}", path):
            return True

        if "calendar" in path:
            return True

        if "date=" in parsed.query:
            return True

        if "sessionid=" in parsed.query:
            return True

        return False

    @staticmethod
    def clean_url(url):

        url = URLUtils.normalize_url(url)

        if not url:
            return None

        if not URLUtils.is_valid_scheme(url):
            return None

        if URLUtils.is_media_file(url):
            return None

        if URLUtils.is_probable_trap(url):
            return None

        return url