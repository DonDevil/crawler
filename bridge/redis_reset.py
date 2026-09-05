"""Shared full-namespace Redis reset helper for the bridge's own
entrypoints (`bridge/main.py --clear-db`, `bridge/result_consumer_main.py
--clear-db`).

Both bridge processes share one physical Redis instance/db with the
crawler's own evidence store *and* with the fingerprinter's own
`fingerprint:*` keyspace -- confirmed the same connection info in
`fingerprint_result_consumer.py::_blocking_read_client`'s docstring ("Same
host/port/db/decoding as `store.redis_conn`... confirmed the same physical
Redis instance"). So a bridge-side `--clear-db` needs to sweep both
prefixes for a clean run.

This repo deliberately never imports the sibling fingerprinter repo's code
(see `fingerprint_stream_adapter.py`'s module docstring -- a real Python
import would pull in that repo's torch/transformers venv, and the two are
meant to stay independently deployable), so `clear_namespace()` below is a
small, standalone SCAN+DELETE rather than a call into
`fingerprinter/work_queue/admin.py::clear_all`. It intentionally mirrors
that function's behavior for the `fingerprint:*` sweep: excluding
`fingerprint:target:*` / `fingerprint:lock:*` by default, since registered
targets and their embeddings are reference data the fingerprinter's own
operator CLI manages, not per-run state the bridge should ever touch.
"""
from __future__ import annotations

import redis

EVIDENCE_NAMESPACE = "evidence"
FINGERPRINT_NAMESPACE = "fingerprint"

# Kept in sync by hand with fingerprinter/work_queue/admin.py::_TARGET_SEGMENTS
# -- see this module's docstring for why the bridge excludes them too.
_FINGERPRINT_TARGET_SEGMENTS = ("target:", "lock:")


def clear_namespace(redis_conn: "redis.Redis", namespace: str, *, exclude_prefixes: tuple = ()) -> int:
    """Delete every key under `{namespace}:*`, skipping any key starting
    with one of `exclude_prefixes`. Testing/reset only -- uses SCAN, the
    same pattern as `RedisURLFrontier.clear()` /
    `RedisMediaEvidenceStore.clear()` (never on a hot path)."""
    pattern = f"{namespace}:*"
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = redis_conn.scan(cursor=cursor, match=pattern, count=200)
        if keys:
            if exclude_prefixes:
                keys = [key for key in keys if not key.startswith(exclude_prefixes)]
            if keys:
                deleted += redis_conn.delete(*keys)
        if cursor == 0:
            break
    return deleted


def clear_fingerprint_namespace(redis_conn: "redis.Redis", *, include_targets: bool = False) -> int:
    """Clear the fingerprinter's `fingerprint:*` run state that the bridge
    forwards jobs into / reads results from. Excludes registered
    targets/embeddings/locks unless `include_targets=True` -- see module
    docstring."""
    exclude_prefixes = () if include_targets else tuple(
        f"{FINGERPRINT_NAMESPACE}:{segment}" for segment in _FINGERPRINT_TARGET_SEGMENTS
    )
    return clear_namespace(redis_conn, FINGERPRINT_NAMESPACE, exclude_prefixes=exclude_prefixes)
