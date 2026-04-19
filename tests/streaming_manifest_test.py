"""Tests for HLS and DASH manifest expansion and evidence storage."""

from parsers.streaming_manifest_parser import StreamingManifestParser
from storage.media_evidence_database import MediaEvidenceDatabase


def test_hls_manifest_parser_extracts_variant_streams():
    parser = StreamingManifestParser()
    manifest = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=640x360
low/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2560000,RESOLUTION=1280x720
https://cdn.example/high/playlist.m3u8
"""

    result = parser.parse_manifest(
        manifest,
        manifest_url="https://cdn.example/master.m3u8",
    )

    assert result["manifest_type"] == "hls"
    assert len(result["variants"]) == 2
    assert result["variants"][0]["url"] == "https://cdn.example/low/playlist.m3u8"
    assert result["variants"][1]["resolution"] == "1280x720"


def test_dash_manifest_parser_extracts_representations():
    parser = StreamingManifestParser()
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v1" bandwidth="1200000" width="640" height="360">
        <BaseURL>video/360p.mp4</BaseURL>
      </Representation>
      <Representation id="v2" bandwidth="2800000" width="1280" height="720">
        <BaseURL>https://cdn.example/video/720p.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

    result = parser.parse_manifest(
        manifest,
        manifest_url="https://cdn.example/manifest.mpd",
    )

    assert result["manifest_type"] == "dash"
    assert len(result["variants"]) == 2
    assert result["variants"][0]["url"] == "https://cdn.example/video/360p.mp4"
    assert result["variants"][1]["bandwidth"] == 2800000


def test_media_evidence_database_stores_manifest_variants(tmp_path):
    db_path = tmp_path / "media_evidence.db"
    database = MediaEvidenceDatabase(path=str(db_path))

    try:
        asset_id = database.record_media_link(
            url="https://cdn.example/master.m3u8",
            source_page="https://piracy.example/watch/film",
            discovered_by="playwright",
            discovery_method="network-response",
            media_type="stream-manifest",
        )
        database.record_manifest_variants(
            asset_id,
            [
                {"url": "https://cdn.example/low/playlist.m3u8", "bandwidth": 1280000, "resolution": "640x360"},
                {"url": "https://cdn.example/high/playlist.m3u8", "bandwidth": 2560000, "resolution": "1280x720"},
            ],
        )

        variants = database.list_manifest_variants(asset_id)
        assert len(variants) == 2
        assert variants[0]["variant_url"] == "https://cdn.example/low/playlist.m3u8"
        assert variants[1]["bandwidth"] == 2560000
    finally:
        database.close()
