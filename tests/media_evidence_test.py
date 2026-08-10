"""Tests for media evidence storage and fingerprint job queue preparation."""

from parsers.html_link_extractor import HTMLLinkExtractor
from storage.media_evidence_store import JOB_QUEUED
from storage.sqlite_media_evidence_store import SQLiteMediaEvidenceStore


def test_media_evidence_store_records_observations_and_fingerprint_jobs(tmp_path):
    db_path = tmp_path / "media_evidence.db"
    store = SQLiteMediaEvidenceStore(path=str(db_path))

    try:
        asset_id = store.record_media_link(
            url="https://cdn.example/movie/master.m3u8",
            source_page="https://piracy.example/watch/movie-123",
            referrer_url="https://piracy.example/",
            discovered_by="playwright",
            discovery_method="network-response",
            media_type="stream-manifest",
            mime_type="application/vnd.apple.mpegurl",
            priority=4,
        )
        duplicate_asset_id = store.record_media_link(
            url="https://cdn.example/movie/master.m3u8",
            source_page="https://piracy.example/watch/movie-123",
            referrer_url="https://piracy.example/embed/123",
            discovered_by="async",
            discovery_method="video-tag",
            media_type="stream-manifest",
            mime_type="application/vnd.apple.mpegurl",
            priority=6,
        )

        assert asset_id == duplicate_asset_id

        assets = store.list_media_assets()
        jobs = store.get_fingerprint_jobs(statuses=[JOB_QUEUED])
        observations = store.list_observations(asset_id)

        assert len(assets) == 1
        assert assets[0]["url"] == "https://cdn.example/movie/master.m3u8"
        assert assets[0]["media_type"] == "stream-manifest"
        assert assets[0]["status"] == "queued_for_fingerprint"

        assert len(jobs) == 1
        assert jobs[0]["asset_id"] == asset_id
        assert jobs[0]["status"] == JOB_QUEUED
        # Rediscovery only ratchets priority down (MIN), never up.
        assert jobs[0]["priority"] == 4

        assert len(observations) == 2
        assert {row["discovered_by"] for row in observations} == {"playwright", "async"}
    finally:
        store.close()


def test_rediscovery_does_not_create_a_second_job(tmp_path):
    db_path = tmp_path / "media_evidence.db"
    store = SQLiteMediaEvidenceStore(path=str(db_path))

    try:
        asset_id = store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=10)

        job = store.claim_next_fingerprint_job(worker_id="w1")
        assert job is not None and job.asset_id == asset_id

        # Rediscovering a claimed asset must not disturb the job's status.
        store.record_media_link(url="https://cdn.example/movie.mp4", media_type="video", priority=5)

        jobs = store.get_fingerprint_jobs()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "claimed"
    finally:
        store.close()


def test_html_link_extractor_separates_navigation_from_media_links():
    html = """
    <html>
      <body>
        <a href="/watch/movie-123">watch page</a>
        <a href="https://cdn.example/movie.mp4">download</a>
        <video src="/streams/master.m3u8">
          <source src="/audio/theme.mp3" type="audio/mpeg" />
        </video>
      </body>
    </html>
    """

    extractor = HTMLLinkExtractor()
    content = extractor.extract_content(html, "https://piracy.example")

    assert "https://piracy.example/watch/movie-123" in content["links"]

    media_urls = {item["url"] for item in content["media_links"]}
    assert "https://cdn.example/movie.mp4" in media_urls
    assert "https://piracy.example/streams/master.m3u8" in media_urls
    assert "https://piracy.example/audio/theme.mp3" in media_urls
    assert "https://cdn.example/movie.mp4" not in content["links"]
