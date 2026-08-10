"""Backward-compatible alias for the pre-Phase-1 module path.

`MediaEvidenceDatabase` was renamed to `SQLiteMediaEvidenceStore`
(storage/sqlite_media_evidence_store.py) as part of the distributed Media
Evidence redesign -- see docs/architecture/media-evidence-step1.md. This
module is kept only so any out-of-tree/older import of
`storage.media_evidence_database.MediaEvidenceDatabase` keeps working; no
new code in this repository should import from here.
"""

from __future__ import annotations

from storage.sqlite_media_evidence_store import SQLiteMediaEvidenceStore as MediaEvidenceDatabase

__all__ = ["MediaEvidenceDatabase"]
