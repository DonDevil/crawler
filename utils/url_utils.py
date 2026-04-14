from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urldefrag, unquote, urlparse, urlunparse


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
    def has_valid_host(url):

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False

            hostname = hostname.lower()

            if hostname == "localhost":
                return True

            try:
                ipaddress.ip_address(hostname)
                return True
            except ValueError:
                pass

            if "." not in hostname:
                return False

            return True

        except Exception:
            return False

    @staticmethod
    def is_onion_url(url):

        try:
            parsed = urlparse(url)
            return bool(parsed.hostname and parsed.hostname.lower().endswith(".onion"))
        except Exception:
            return False

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

        decoded = unquote(url)
        if any(token in decoded for token in ("<", ">", '"', "{", "}")):
            return True

        return False

    @staticmethod
    def clean_url(url):

        url = URLUtils.normalize_url(url)

        if not url:
            return None

        if not URLUtils.is_valid_scheme(url):
            return None

        if not URLUtils.has_valid_host(url):
            return None

        if URLUtils.is_media_file(url):
            return None

        if URLUtils.is_probable_trap(url):
            return None

        return url