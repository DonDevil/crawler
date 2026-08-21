"""Which registered fingerprinter target a crawler run is protecting.

Phase 3 (docs/architecture/phase-3-crawler-fingerprinter-bridge.md) found
that the crawler has no legitimate source of `target_id`/`target_version`
-- both mandatory, non-empty fields on the fingerprinter's
`FingerprintCandidate`/`Job` contract (integration/candidate.py,
work_queue/jobs.py in the sibling fingerprinter repo) -- and that
fabricating either would be worse than not submitting a job at all (a
worker would burn a DINOv2 inference pass on a job guaranteed to fail with
`KeyError: unknown target`).

This module is the crawler-side half of the fix: it does not register
targets (that remains the fingerprinter's `target.registry.TargetRegistry
.register_target()`, run via the sibling repo's `scripts/register_target.py`
-- the fingerprinter's own registry is the sole source of truth, see that
module's docstring) and it does not implement the crawler->fingerprinter
bridge (still deferred, Phase 3 doc). It only lets one *already-registered*
target be named as the explicit scope of one crawler run, and validates
that naming before any crawling starts.

Deliberately excluded, per the phase brief: no target is ever inferred from
a media URL, filename, search query, or discovered title -- `TargetScope`
only ever comes from configuration/CLI, and `verify_target_registered` only
ever confirms a scope that was already explicitly given, never invents or
substitutes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import redis
from loguru import logger

# The fingerprinter's own key convention for a registered target record
# (sibling repo: target/keys.py::target_key, target/registry.py
# ::TargetRegistry.register_target). Namespace lives entirely inside the
# fingerprinter repo -- this crawler repo does not own it and must not
# reinvent its shape, only read it, per the documented cross-repo contract
# (docs/architecture/phase-3-crawler-fingerprinter-bridge.md §4/§7). Deliberately
# NOT built from RedisMediaEvidenceStore._key()/self.namespace: the
# `fingerprint:*` prefix is fixed by the fingerprinter, independent of this
# repo's own `evidence.redis_namespace` configuration.
_FINGERPRINT_TARGET_KEY_TEMPLATE = "fingerprint:target:{target_id}:{target_version}"


class TargetScopeError(ValueError):
    """A configured target scope is malformed (e.g. only one of
    `target_id`/`target_version` was given) -- never raised for "no scope
    configured at all", only for a scope that was partially/incorrectly
    specified."""


class TargetNotRegisteredError(RuntimeError):
    """A configured target scope is well-formed but does not correspond to
    any target registered in the fingerprinter's `TargetRegistry`. Never
    silently substituted or ignored -- the caller must fail startup."""


@dataclass(frozen=True)
class TargetScope:
    """The one registered (target_id, target_version) pair a crawler run is
    protecting. Both fields are opaque identifiers assigned by whoever ran
    the fingerprinter's registration tooling -- never derived from a URL,
    filename, discovered title, or crawler asset id (see module docstring).
    """

    target_id: str
    target_version: str

    def __post_init__(self) -> None:
        if not self.target_id or not self.target_version:
            raise TargetScopeError(
                "TargetScope requires both target_id and target_version to be non-empty "
                f"(got target_id={self.target_id!r}, target_version={self.target_version!r})"
            )


def resolve_target_scope(target_id: Optional[str], target_version: Optional[str]) -> Optional[TargetScope]:
    """Turn config/CLI-supplied `target_id`/`target_version` into a
    `TargetScope`, or `None` if a crawler run has no target scope at all
    (today's default, and today's only behavior before this phase --
    unscoped runs still record media evidence and create fingerprint jobs
    exactly as before, just with no target association).

    Raises `TargetScopeError` if exactly one of the two was given -- fails
    before any Redis round trip, since this is a static configuration
    mistake, not a "does the target exist" question (see
    `verify_target_registered` for that).
    """
    if not target_id and not target_version:
        return None
    if not target_id or not target_version:
        raise TargetScopeError(
            "target_id and target_version must both be set, or both left unset -- "
            f"got target_id={target_id!r}, target_version={target_version!r}"
        )
    return TargetScope(target_id=target_id, target_version=target_version)


def verify_target_registered(redis_conn: "redis.Redis", scope: TargetScope) -> bool:
    """Read-only existence check against the fingerprinter's own
    `TargetRegistry` record for `scope`, using its documented Redis key
    convention (see module docstring) -- never the crawler's own
    `evidence:*` keyspace, and never a write.

    This is deliberately a single `EXISTS`, not a field-by-field read: this
    module has no business interpreting `TargetRecord`'s internal shape
    (`content_sha256`, `media_metadata`, ...) -- that remains entirely
    inside the fingerprinter repo. "Does this key exist" is the one fact a
    crawler run needs before it starts creating jobs that will eventually
    reference this target.

    Raises whatever `redis.RedisError` the connection raises (e.g.
    `redis.ConnectionError`) -- an infrastructure failure here must not be
    read as "target not found" (docs/architecture/phase-3-crawler-fingerprinter-bridge.md's
    "infrastructure vs. candidate-specific failure" distinction applies
    here too).
    """
    key = _FINGERPRINT_TARGET_KEY_TEMPLATE.format(target_id=scope.target_id, target_version=scope.target_version)
    exists = bool(redis_conn.exists(key))
    if not exists:
        logger.warning(
            f"Target scope {scope.target_id!r}/{scope.target_version!r} is not registered "
            f"in the fingerprinter's TargetRegistry (checked {key!r}); register it first "
            "(see the sibling fingerprinter repo's scripts/register_target.py)."
        )
    return exists
