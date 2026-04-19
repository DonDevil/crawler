"""Parse HLS and DASH manifests into structured streaming evidence."""

from __future__ import annotations

from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from utils.url_utils import URLUtils


class StreamingManifestParser:
    """Extract variant stream URLs from HLS and DASH manifests."""

    def parse_manifest(self, manifest_text: str, manifest_url: str) -> dict:
        lowered = (manifest_text or "").lower()
        if "#extm3u" in lowered:
            return self._parse_hls(manifest_text, manifest_url)
        if "<mpd" in lowered:
            return self._parse_dash(manifest_text, manifest_url)
        return {
            "manifest_type": "unknown",
            "manifest_url": URLUtils.clean_media_url(manifest_url),
            "variants": [],
        }

    def _parse_hls(self, manifest_text: str, manifest_url: str) -> dict:
        variants: list[dict] = []
        pending_stream_inf: dict | None = None

        for raw_line in manifest_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("#EXT-X-STREAM-INF:"):
                pending_stream_inf = self._parse_hls_attributes(line.partition(":")[2])
                continue

            if line.startswith("#"):
                continue

            full_url = URLUtils.clean_media_url(urljoin(manifest_url, line))
            if not full_url:
                continue

            if pending_stream_inf is not None:
                variants.append(
                    {
                        "url": full_url,
                        "media_type": "stream-manifest",
                        "bandwidth": self._safe_int(pending_stream_inf.get("BANDWIDTH")),
                        "resolution": pending_stream_inf.get("RESOLUTION"),
                        "codecs": pending_stream_inf.get("CODECS"),
                    }
                )
                pending_stream_inf = None

        return {
            "manifest_type": "hls",
            "manifest_url": URLUtils.clean_media_url(manifest_url),
            "variants": variants,
        }

    def _parse_dash(self, manifest_text: str, manifest_url: str) -> dict:
        variants: list[dict] = []
        root = ET.fromstring(manifest_text)

        for representation in root.findall(".//{*}Representation"):
            base_url_element = representation.find("{*}BaseURL")
            if base_url_element is None or not (base_url_element.text or "").strip():
                continue

            full_url = URLUtils.clean_media_url(urljoin(manifest_url, base_url_element.text.strip()))
            if not full_url:
                continue

            width = representation.attrib.get("width")
            height = representation.attrib.get("height")
            resolution = f"{width}x{height}" if width and height else None

            variants.append(
                {
                    "url": full_url,
                    "media_type": URLUtils.classify_media_url(full_url, None),
                    "bandwidth": self._safe_int(representation.attrib.get("bandwidth")),
                    "resolution": resolution,
                    "codecs": representation.attrib.get("codecs"),
                }
            )

        return {
            "manifest_type": "dash",
            "manifest_url": URLUtils.clean_media_url(manifest_url),
            "variants": variants,
        }

    @staticmethod
    def _parse_hls_attributes(attribute_text: str) -> dict[str, str]:
        attributes: dict[str, str] = {}
        for part in attribute_text.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            attributes[key.strip().upper()] = value.strip().strip('"')
        return attributes

    @staticmethod
    def _safe_int(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
