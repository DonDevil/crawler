from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urldefrag, unquote, urlparse, urlunparse

try:
    import tldextract
    _TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=None)
except Exception:  # pragma: no cover
    tldextract = None
    _TLD_EXTRACTOR = None


TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".webm",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ogv",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".aac",
    ".flac",
    ".ogg",
    ".m4a",
    ".opus",
}

STREAMING_EXTENSIONS = {
    ".m3u8",
    ".mpd",
    ".m3u",
    ".ts",
    ".m4s",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
}

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".rar",
    ".tar",
    ".gz",
}

UNWANTED_EXTENSIONS = (
    IMAGE_EXTENSIONS
    | VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
    | STREAMING_EXTENSIONS
    | DOCUMENT_EXTENSIONS
    | ARCHIVE_EXTENSIONS
)

AUTO_BLACKLIST_DEFAULTS = {
    "wikipedia.org",
    "wikimedia.org",
    "wikidata.org",
    "imdb.com",
    "rottentomatoes.com",
    "metacritic.com",
    "letterboxd.com",
    "bookmyshow.com",
    "justwatch.com",
    "hindustantimes.com",
    "thehindu.com",
    "indiatoday.in",
    "indianexpress.com",
    "ndtv.com",
    "news18.com",
    "msn.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "reddit.com",
    "linkedin.com",
    "quora.com",
    "pinterest.com",
    "tiktok.com",
    "telegram.org",
    "t.me",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adnxs.com",
    "popads.net",
    "propellerads.com",
    "exoclick.com",
    "taboola.com",
    "outbrain.com",
    "mgid.com",
}

AD_HOST_HINT_PATTERNS = (
    re.compile(r"(^|[._-])(adservice|adserver|adclick|adtrack|adsystem|advert|popunder|popup|banner)([._-]|$)"),
    re.compile(r"(^|[._-])(track|tracker|tracking|redirect|click)([._-]|$)"),
)

CONTENT_PATH_HINTS = (
    "/watch",
    "/stream",
    "/download",
    "/movie",
    "/episode",
    "/play",
)

RELEVANT_EXTERNAL_HINTS = {
    "torrent",
    "pirate",
    "stream",
    "watch",
    "movie",
    "movies",
    "series",
    "episode",
    "download",
    "magnet",
    "subtitle",
    "subtitles",
    "player",
    "video",
    "media",
    "file",
    "share",
    "ddl",
    "anime",
}

LOW_SIGNAL_PATH_HINTS = (
    "/about",
    "/contact",
    "/privacy",
    "/terms",
    "/login",
    "/signup",
    "/register",
    "/account",
    "/cookie",
    "/advert",
    "/sponsor",
    "/faq",
)

ADULT_TOKEN_HINTS = {
    "porn",
    "porno",
    "sex",
    "xxx",
    "adult",
    "erotic",
    "hentai",
    "nsfw",
    "nude",
    "nudes",
}

ADULT_SUBSTRING_HINTS = (
    "pornhub",
    "youporn",
    "redtube",
    "xvideos",
    "xnxx",
    "xhamster",
    "brazzers",
    "onlyfans",
)

AUTO_BLACKLIST_HINTS = {
    "wikipedia",
    "wikimedia",
    "wikidata",
    "imdb",
    "rottentomatoes",
    "metacritic",
    "letterboxd",
    "bookmyshow",
    "justwatch",
    "hindustantimes",
    "thehindu",
    "indiatoday",
    "indianexpress",
    "ndtv",
    "news18",
    "timesofindia",
    "msn",
    "facebook",
    "instagram",
    "twitter",
    "youtube",
    "reddit",
    "linkedin",
    "quora",
    "pinterest",
    "tiktok",
    "telegram",
}


class URLUtils:

    _blacklist_path = Path("datasets/domain_blacklist.txt")
    _blacklist_enabled = True
    _blacklist_domains: set[str] = set()
    _blacklist_mtime_ns: int | None = None

    @classmethod
    def set_blacklist_path(cls, path: str) -> None:
        cls._blacklist_path = Path(path)
        cls._blacklist_domains = set()
        cls._blacklist_mtime_ns = None

    @classmethod
    def set_blacklist_enabled(cls, enabled: bool) -> None:
        cls._blacklist_enabled = enabled
        if enabled:
            cls.ensure_blacklist_seeded()

    @classmethod
    def _ensure_blacklist_file_exists(cls) -> None:
        cls._blacklist_path.parent.mkdir(parents=True, exist_ok=True)
        cls._blacklist_path.touch(exist_ok=True)

    @classmethod
    def _extract_registered_domain(cls, url_or_domain: str) -> str | None:
        try:
            parsed = urlparse(url_or_domain if "://" in url_or_domain else f"http://{url_or_domain}")
            hostname = (parsed.hostname or url_or_domain).lower().strip().strip(".")
            if not hostname:
                return None

            if hostname == "localhost":
                return hostname

            try:
                ipaddress.ip_address(hostname)
                return hostname
            except ValueError:
                pass

            if _TLD_EXTRACTOR is not None:
                extracted = _TLD_EXTRACTOR(hostname)
                registered = getattr(extracted, "top_domain_under_public_suffix", "")
                suffix = getattr(extracted, "suffix", "")
                if registered in {"example", "invalid", "test", "localhost", "local"}:
                    registered = hostname
                if not registered:
                    domain = getattr(extracted, "domain", "")
                    if not suffix and "." in hostname:
                        registered = hostname
                    elif suffix in {"example", "invalid", "test", "localhost", "local"}:
                        registered = hostname
                    else:
                        registered = f"{domain}.{suffix}" if domain and suffix else domain
                if registered:
                    return registered.lower()

            return hostname
        except Exception:
            return None

    @classmethod
    def ensure_blacklist_seeded(cls) -> None:
        if not cls._blacklist_enabled:
            return

        cls._ensure_blacklist_file_exists()
        cls._reload_blacklist_if_needed()

        missing = [domain for domain in sorted(AUTO_BLACKLIST_DEFAULTS) if domain not in cls._blacklist_domains]
        if not missing:
            return

        existing_text = cls._blacklist_path.read_text(encoding="utf-8")
        with open(cls._blacklist_path, "a", encoding="utf-8") as handle:
            if not existing_text.strip():
                handle.write("# Auto-populated non-target domains for anti-piracy crawling\n")
            elif not existing_text.endswith("\n"):
                handle.write("\n")

            for domain in missing:
                handle.write(f"{domain}\n")

        cls._blacklist_mtime_ns = None
        cls._reload_blacklist_if_needed()

    @classmethod
    def add_to_blacklist(cls, url_or_domain: str) -> bool:
        if not cls._blacklist_enabled:
            return False

        cls._ensure_blacklist_file_exists()
        cls._reload_blacklist_if_needed()

        domain = cls._extract_registered_domain(url_or_domain)
        if not domain or domain in cls._blacklist_domains:
            return False

        with open(cls._blacklist_path, "a", encoding="utf-8") as handle:
            existing_text = cls._blacklist_path.read_text(encoding="utf-8")
            if existing_text and not existing_text.endswith("\n"):
                handle.write("\n")
            handle.write(f"{domain}\n")

        cls._blacklist_mtime_ns = None
        cls._reload_blacklist_if_needed()
        return True

    @classmethod
    def is_adult_content_url(cls, url: str) -> bool:
        hostname = cls.extract_domain(url) or ""
        registered = cls._extract_registered_domain(url) or hostname
        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        query = (parsed.query or "").lower()
        haystack = f"{hostname} {registered} {path} {query}".lower()
        tokens = {token for token in re.split(r"[^a-z0-9]+", haystack) if token}

        if tokens.intersection(ADULT_TOKEN_HINTS):
            return True

        return any(hint in haystack for hint in ADULT_SUBSTRING_HINTS)

    @classmethod
    def should_auto_blacklist(cls, url: str) -> bool:
        hostname = cls.extract_domain(url) or ""
        registered = cls._extract_registered_domain(url) or hostname
        haystack = f"{hostname} {registered}".lower()

        if cls.is_adult_content_url(url):
            return True

        if registered in AUTO_BLACKLIST_DEFAULTS or hostname in AUTO_BLACKLIST_DEFAULTS:
            return True

        if cls.is_probable_ad_domain(url):
            return True

        return any(token in haystack for token in AUTO_BLACKLIST_HINTS)

    @classmethod
    def same_registered_domain(cls, left: str, right: str) -> bool:
        left_domain = cls._extract_registered_domain(left)
        right_domain = cls._extract_registered_domain(right)
        return bool(left_domain and right_domain and left_domain == right_domain)

    @classmethod
    def is_probable_ad_domain(cls, url_or_domain: str) -> bool:
        hostname = cls.extract_domain(url_or_domain) or ""
        registered = cls._extract_registered_domain(url_or_domain) or hostname
        haystack = f"{hostname} {registered}".lower()

        if registered in AUTO_BLACKLIST_DEFAULTS or hostname in AUTO_BLACKLIST_DEFAULTS:
            return True

        return any(pattern.search(hostname) for pattern in AD_HOST_HINT_PATTERNS)

    @classmethod
    def is_suspicious_redirect(cls, source_url: str, final_url: str) -> bool:
        if not source_url or not final_url:
            return False

        if cls.same_registered_domain(source_url, final_url):
            return False

        final_registered = cls._extract_registered_domain(final_url) or ""
        if not final_registered:
            return False

        if cls.is_probable_ad_domain(final_url) or cls.is_blacklisted(final_url):
            return True

        source_path = (urlparse(source_url).path or "").lower()
        if any(token in source_path for token in CONTENT_PATH_HINTS):
            return True

        return False

    @classmethod
    def is_likely_piracy_target(cls, url: str) -> bool:
        hostname = cls.extract_domain(url) or ""
        registered = cls._extract_registered_domain(url) or hostname
        path = (urlparse(url).path or "/").lower()
        haystack = f"{hostname} {registered} {path}".lower()
        tokens = {token for token in re.split(r"[^a-z0-9]+", haystack) if token}

        if cls.is_onion_url(url):
            return True

        if tokens.intersection(RELEVANT_EXTERNAL_HINTS):
            return True

        return any(token in path for token in CONTENT_PATH_HINTS)

    @classmethod
    def should_queue_link(cls, source_url: str, target_url: str, *, from_text: bool = False) -> bool:
        if not target_url:
            return False

        if cls.is_adult_content_url(target_url):
            return False

        if cls.is_probable_ad_domain(target_url) or cls.is_blacklisted(target_url):
            return False

        if cls.same_registered_domain(source_url, target_url):
            return True

        if cls.is_onion_url(target_url):
            return True

        path = (urlparse(target_url).path or "/").lower()
        if any(token in path for token in LOW_SIGNAL_PATH_HINTS):
            return False

        if cls.is_likely_piracy_target(target_url):
            return True

        return False

    @classmethod
    def get_link_priority(cls, source_url: str, target_url: str) -> int:
        if cls.same_registered_domain(source_url, target_url):
            return 8

        if cls.is_onion_url(target_url):
            return 9

        if cls.is_likely_piracy_target(target_url):
            return 11

        if cls.is_probable_ad_domain(target_url) or cls.is_blacklisted(target_url):
            return 50

        return 20

    @classmethod
    def _reload_blacklist_if_needed(cls) -> None:
        if not cls._blacklist_enabled:
            return

        try:
            stat = cls._blacklist_path.stat()
        except FileNotFoundError:
            cls._blacklist_domains = set()
            cls._blacklist_mtime_ns = None
            return

        if cls._blacklist_mtime_ns == stat.st_mtime_ns:
            return

        domains: set[str] = set()
        with open(cls._blacklist_path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip().lower()
                if not line or line.startswith("#"):
                    continue

                parsed = urlparse(line if "://" in line else f"http://{line}")
                host = (parsed.hostname or line).lower().strip().strip(".")
                if host:
                    domains.add(host)

        cls._blacklist_domains = domains
        cls._blacklist_mtime_ns = stat.st_mtime_ns

    @classmethod
    def is_blacklisted(cls, url: str) -> bool:
        if not cls._blacklist_enabled:
            return False

        cls.ensure_blacklist_seeded()
        if cls.should_auto_blacklist(url):
            cls.add_to_blacklist(url)

        cls._reload_blacklist_if_needed()
        if not cls._blacklist_domains:
            return False

        try:
            parsed = urlparse(url if "://" in url else f"http://{url}")
            hostname = (parsed.hostname or "").lower().strip().strip(".")
        except Exception:
            return False

        if not hostname:
            return False

        return any(hostname == blocked or hostname.endswith(f".{blocked}") for blocked in cls._blacklist_domains)

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
            return (parsed.hostname or parsed.netloc).lower()
        except Exception:
            return None

    @staticmethod
    def looks_like_media_content_type(content_type: str | None) -> bool:
        if not content_type:
            return False

        lowered = content_type.lower()
        return any(token in lowered for token in (
            "video/",
            "audio/",
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
            "application/dash+xml",
            "application/octet-stream",
        ))

    @staticmethod
    def classify_media_url(url: str, content_type: str | None = None) -> str:
        lowered_url = (url or "").lower()
        lowered_type = (content_type or "").lower()

        if any(token in lowered_type for token in ("mpegurl", "dash+xml")) or any(lowered_url.endswith(ext) for ext in STREAMING_EXTENSIONS):
            return "stream-manifest"
        if lowered_type.startswith("video/") or any(lowered_url.endswith(ext) for ext in VIDEO_EXTENSIONS):
            return "video"
        if lowered_type.startswith("audio/") or any(lowered_url.endswith(ext) for ext in AUDIO_EXTENSIONS):
            return "audio"
        if any(lowered_url.endswith(ext) for ext in DOCUMENT_EXTENSIONS):
            return "document"
        if any(lowered_url.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            return "archive"
        return "unknown"

    @staticmethod
    def clean_media_url(url, apply_blacklist: bool = True):

        url = URLUtils.normalize_url(url)

        if not url:
            return None

        if not URLUtils.is_valid_scheme(url):
            return None

        if not URLUtils.has_valid_host(url):
            return None

        if URLUtils.is_adult_content_url(url):
            return None

        if apply_blacklist and URLUtils.is_blacklisted(url):
            return None

        if URLUtils.is_probable_trap(url):
            return None

        return url

    @staticmethod
    def is_media_file(url):

        media_type = URLUtils.classify_media_url(url)
        return media_type != "unknown"

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
    def clean_url(url, apply_blacklist: bool = True):

        url = URLUtils.normalize_url(url)

        if not url:
            return None

        if not URLUtils.is_valid_scheme(url):
            return None

        if not URLUtils.has_valid_host(url):
            return None

        if URLUtils.is_adult_content_url(url):
            if apply_blacklist and URLUtils._blacklist_enabled:
                URLUtils.add_to_blacklist(url)
            return None

        if apply_blacklist and URLUtils.is_blacklisted(url):
            return None

        if URLUtils.is_media_file(url):
            return None

        if URLUtils.is_probable_trap(url):
            return None

        return url