"""Minimal Redis-native adapter for the fingerprinter's existing result
contract -- `work_queue.results.Result`/`ResultRecord` and their `keys.py`
conventions, all in the sibling `fingerprinter` repository.

Mirrors `bridge/fingerprint_stream_adapter.py`'s existing rationale exactly,
just for the reverse (fingerprinter -> crawler) direction: the fingerprinter's
result contract is an in-process Python API defined in a separate,
independently-deployed repository with its own virtual environment. Reading
it from here without importing that code means hand-replicating the byte-for-
byte Redis key/field conventions it already uses -- verified against that
repo's actual source (not guessed, not inferred) as of the git revision this
phase inspected it at. If the fingerprinter repo ever changes any of these,
this module must be updated to match by hand -- there is no way to detect
drift across the repository boundary automatically.

Copied from, one-to-one:

    fingerprinter/work_queue/keys.py::result_key, results_stream_key
    fingerprinter/work_queue/results.py::ResultDecision,
        ResultRecord.to_hash_fields, ResultRecord.to_event_fields
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/work_queue/keys.py
# ---------------------------------------------------------------------
_DEFAULT_PRIORITY = "default"


def result_key(job_id: str) -> str:
    return f"fingerprint:job:{job_id}:result"


def results_stream_key(priority: str = _DEFAULT_PRIORITY) -> str:
    return f"fingerprint:results:stream:{priority}"


# This consumer's own Redis consumer group name -- deliberately distinct
# from the fingerprinter's own `"fingerprinter-workers"` group (which reads
# the *jobs* stream, not the *results* stream this module reads). Not a
# copy of anything in the fingerprinter repo.
CONSUMER_GROUP = "crawler-evidence-consumers"


# ---------------------------------------------------------------------
# Copied verbatim from fingerprinter/work_queue/results.py
# ---------------------------------------------------------------------
class ResultDecision:
    MATCH = "match"
    NO_MATCH = "no_match"
    PROCESSING_FAILURE = "processing_failure"


class MalformedResultEventError(ValueError):
    """A `fingerprint:results:stream:*` entry does not carry the required
    fields -- can never become valid via redelivery, so callers should
    reject and XACK it, not retry."""


class MalformedResultRecordError(ValueError):
    """The durable `fingerprint:job:{job_id}:result` hash a result event
    pointed at is missing required fields, or names a decision this adapter
    does not know how to map."""


# Fields the producer always writes (work_queue.results.ResultRecord.
# to_event_fields) -- required to even attempt resolving the full record.
_REQUIRED_EVENT_FIELDS = ("job_id", "media_evidence_id", "target_id", "decision")

# Fields the producer always writes (work_queue.results.ResultRecord.
# to_hash_fields) -- required to build a crawler-side FingerprintResult.
_REQUIRED_RECORD_FIELDS = ("job_id", "media_evidence_id", "target_id", "target_version", "decision", "worker_id")


def parse_result_event(fields: Mapping[str, str]) -> str:
    """Validate a `fingerprint:results:stream:*` entry and return its
    `job_id` -- the only field this adapter trusts from the event itself.
    Every other field (decision, confidence, evidence, ...) is read from
    the authoritative `result_key(job_id)` hash instead, since the event is
    deliberately "just enough to fetch the full durable record"
    (work_queue.results.ResultRecord.to_event_fields's own docstring).

    Raises `MalformedResultEventError` for a missing required field.
    """
    missing = [name for name in _REQUIRED_EVENT_FIELDS if not fields.get(name)]
    if missing:
        raise MalformedResultEventError(f"result event missing required field(s): {', '.join(missing)}")
    return fields["job_id"]


@dataclass(frozen=True)
class ResolvedResult:
    """Everything a crawler-side caller needs from one fingerprinter
    result, read from `result_key(job_id)` -- the single authoritative
    source for all of it. Deliberately does NOT carry `media_url`/
    `source_domain`: those belong to the crawler's own `evidence:asset:
    {media_evidence_id}` record and must never be sourced from the
    fingerprinter's copy (there isn't one in this hash anyway)."""

    job_id: str
    media_evidence_id: str
    target_id: str
    target_version: str
    decision: str
    worker_id: str
    confidence: Optional[float] = None
    algorithm: Optional[str] = None
    summary: Optional[str] = None
    evidence: Optional[str] = None
    processing_completed_at: Optional[float] = None


def parse_result_record(job_id: str, fields: Mapping[str, str]) -> ResolvedResult:
    """Parse `result_key(job_id)`'s hash (already fetched by the caller)
    into a `ResolvedResult`. Raises `MalformedResultRecordError` for a
    missing required field."""
    missing = [name for name in _REQUIRED_RECORD_FIELDS if not fields.get(name)]
    if missing:
        raise MalformedResultRecordError(
            f"result record for job_id={job_id!r} missing required field(s): {', '.join(missing)}"
        )
    if fields["job_id"] != job_id:
        raise MalformedResultRecordError(
            f"result record job_id={fields['job_id']!r} does not match requested job_id={job_id!r}"
        )

    confidence = fields.get("confidence")
    completed_at = fields.get("processing_completed_at")
    return ResolvedResult(
        job_id=job_id,
        media_evidence_id=fields["media_evidence_id"],
        target_id=fields["target_id"],
        target_version=fields["target_version"],
        decision=fields["decision"],
        worker_id=fields["worker_id"],
        confidence=float(confidence) if confidence not in (None, "") else None,
        algorithm=fields.get("algorithm") or None,
        summary=fields.get("summary") or None,
        evidence=fields.get("evidence") or None,
        processing_completed_at=float(completed_at) if completed_at not in (None, "") else None,
    )
