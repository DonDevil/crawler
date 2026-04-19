"""Tests for hashing queue consumption and pirate-site priority tracking."""

from storage.domain_database import DomainDatabase
from storage.media_evidence_database import MediaEvidenceDatabase


def test_fingerprinter_can_claim_complete_and_match_jobs(tmp_path):
    db_path = tmp_path / "media_evidence.db"
    media_db = MediaEvidenceDatabase(path=str(db_path))

    try:
        asset_id = media_db.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/123",
            discovered_by="async",
            discovery_method="anchor",
            media_type="video",
            priority=3,
        )

        claimed = media_db.claim_next_sample_job(worker_name="fingerprinter-1")
        assert claimed is not None
        assert claimed["asset_id"] == asset_id
        assert claimed["status"] == "claimed"

        media_db.complete_sample_job(
            asset_id,
            fingerprint_status="matched",
            match_confidence=0.97,
            matched_title="Target Movie",
        )

        asset = media_db.list_media_assets()[0]
        assert asset["status"] == "matched"
        assert asset["match_confidence"] == 0.97
        assert asset["matched_title"] == "Target Movie"
    finally:
        media_db.close()


def test_matching_media_increases_source_domain_reputation(tmp_path):
    media_db_path = tmp_path / "media_evidence.db"
    domain_db_path = tmp_path / "domains.db"

    media_db = MediaEvidenceDatabase(path=str(media_db_path))
    domain_db = DomainDatabase(path=str(domain_db_path))

    try:
        asset_id = media_db.record_media_link(
            url="https://cdn.example/movie.mp4",
            source_page="https://piracy.example/watch/123",
            referrer_url="https://piracy.example/",
            discovered_by="playwright",
            discovery_method="network-response",
            media_type="video",
            priority=2,
        )

        domain = media_db.mark_asset_matched(
            asset_id,
            matched_title="Target Movie",
            confidence=0.99,
            domain_database=domain_db,
            score_increment=2.5,
        )

        assert domain == "piracy.example"
        assert domain_db.get_score("piracy.example") == 2.5
    finally:
        media_db.close()
        domain_db.close()
