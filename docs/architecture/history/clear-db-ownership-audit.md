# Redis Namespace Ownership Audit — Why `bridge.main` / `bridge.result_consumer_main --clear-db` Were Removed

Status: **audit + fix. `bridge/redis_reset.py`, both entrypoints' `--clear-db`
flags, and `tests/bridge_redis_reset_test.py` were removed.**

## 1. Incident

Reported: running `bridge.main --clear-db` (or
`bridge.result_consumer_main --clear-db`) while a fingerprinter worker
process (`fingerprinter/worker/main.py`, sibling repo) was already running
crashed that worker.

## 2. Root cause

`bridge/redis_reset.py::clear_fingerprint_namespace()` (now removed) did a
`SCAN`+`DELETE` over every `fingerprint:*` key except
`fingerprint:target:*`/`fingerprint:lock:*` — including
`fingerprint:jobs:stream:{priority}`, the Redis Stream a live fingerprinter
worker holds open via a consumer group (`Worker._ensure_group()` in
`fingerprinter/worker/fingerprint_worker.py`, created once at worker
startup with `XGROUP CREATE ... MKSTREAM`).

`DEL` on a Stream key removes the stream **and every consumer group
registered on it** — there is no way to delete only the entries and keep
the group. The worker's next `claim_one()` (`XREADGROUP`) or
`reclaim_stale()` (`XAUTOCLAIM`) call against that now-nonexistent
stream/group raises `redis.exceptions.ResponseError: NOGROUP No such key
... or consumer group ... in XREADGROUP with GROUP option`. Traced the call
chain in `fingerprinter/worker/main.py`: `worker.run(handler)` (the main
loop) has no `try/except` around it — `ResponseError` propagates straight
up through `main()`'s `try/finally` (the `finally` only emits a shutdown
summary and closes the Redis client; it does not suppress the exception)
and the process exits with a traceback. This is the exact observed crash.

## 3. Was this "fixable" by catching the exception?

Deliberately not attempted. Catching `ResponseError` in the worker's main
loop would hide the crash but not the underlying problem: the worker's
consumer group would still be gone, and every job already claimed by this
worker (in its PEL, pending-entries list) would be silently lost along
with the stream. The bridge/result-consumer must not be able to delete
state the fingerprinter has open in the first place — the fix belongs at
the ownership boundary, not as defensive exception-handling downstream of
a destructive operation crossing it.

## 4. Redis namespace ownership (traced from actual key construction, not filenames)

| Namespace (key prefix) | Schema owner (defines keys, is primary writer) | Also read/written by | Authoritative clear |
|---|---|---|---|
| `crawler:*` (frontier: `urls:known`, `urls:visited`, `domain:*`, `claim:*`, `attempts:*`, `meta:*`, ...) | Crawler (`core/redis_frontier.py`) | Nothing else — the frontier is crawler-internal | `python main.py --clear-db` → `CrawlerManager.clear_storage()` → `RedisURLFrontier.clear()` |
| `evidence:*` (`evidence:asset:*`, `evidence:job:*`, `evidence:jobs:queue`, `evidence:jobs:claim:*`, ...) | Crawler (`storage/redis_media_evidence_store.py`) — crawl-time `record_media_link()` is the primary writer | Bridge (`bridge/crawler_fingerprinter_bridge.py`, `bridge/fingerprint_result_consumer.py`) *claims*/*completes*/*fails* existing `evidence:job:*` records via `RedisMediaEvidenceStore`'s own public methods (`claim_next_fingerprint_job`, `mark_fingerprint_job_forwarded`, `fail_fingerprint_job`, `reclaim_expired_jobs`) — it never defines new key shapes of its own | `python main.py --clear-db` → `CrawlerManager.clear_storage()` → `RedisMediaEvidenceStore.clear()` (already covers this fully — see `docs/installation.md`'s `--clear-db` blast-radius warning) |
| `fingerprint:*` (`fingerprint:jobs:stream:*`, `fingerprint:job:*:state`, `fingerprint:results:stream:*`, `fingerprint:retry:*`, `fingerprint:matches:*`, `fingerprint:target:*`, `fingerprint:lock:*`, submission markers) | Fingerprinter (sibling repo: `work_queue/keys.py`, `target/keys.py`, `integration/keys.py`) | Bridge forwards jobs onto this stream (`bridge/fingerprint_stream_adapter.py`, a same-repo re-implementation of the fingerprinter's own producer contract — deliberately not a cross-repo import, see that module's docstring) and reads results from it (`bridge/fingerprint_result_consumer.py`) — again, exclusively through that contract's own documented wire format, never a key shape the bridge invented | `python -m target.cli clear-db` (sibling fingerprinter repo) → `work_queue/admin.py::clear_all()` |

**The bridge and the result consumer own no namespace of their own.** Every
key either process touches was already covered by one of the two rows
above. `bridge/redis_reset.py` existed only because neither entrypoint
called the crawler's own reset (for `evidence:*`) and neither had a way to
also reach the fingerprinter's `clear-db` (for `fingerprint:*`) —
convenience, not a genuine independent ownership claim (see the removed
`docs/architecture/history/bridge-clear-db.md` for the original
reasoning). That convenience is exactly what crossed the ownership
boundary and crashed a live fingerprinter worker: an unrelated component
(the bridge) was able to destructively reset a schema it doesn't own and
has no visibility into who else currently has it open.

## 5. Was `FLUSHDB`/`FLUSHALL` involved?

No. `bridge/redis_reset.py::clear_namespace()` (like every other clear
implementation in both repos —
`RedisURLFrontier.clear()`/`RedisMediaEvidenceStore.clear()`/
`work_queue/admin.py::clear_all()`) used `SCAN` + batched `DELETE`, never
`FLUSHDB`/`FLUSHALL`/`KEYS`. The danger here was never "too broad a
primitive" — a namespace-scoped `SCAN`+`DELETE` is the right primitive and
stays. The danger was *whose* namespace an unrelated process was allowed
to point that primitive at.

## 6. Fix

Removed, not narrowed — the audit found no bridge/result-consumer-owned
state to preserve a scoped reset for:

- `bridge/redis_reset.py` deleted (`clear_namespace()`/
  `clear_fingerprint_namespace()` had no other callers).
- `--clear-db` removed from `bridge/main.py` and
  `bridge/result_consumer_main.py` (flag, help text, and the
  `store.clear()` + `clear_fingerprint_namespace()` call it triggered).
- `tests/bridge_redis_reset_test.py` deleted (tested only the removed
  module).
- `docs/architecture/bridge-clear-db.md` moved to
  `docs/architecture/history/bridge-clear-db.md` with a superseded banner
  pointing here.

A full-pipeline reset is now two independent, order-independent calls —
not three:

```bash
# Crawler-owned state: URL frontier + evidence:*
python main.py --clear-db

# Fingerprinter-owned state: fingerprint:* (jobs/results/retries/matches/
# submission markers; registered targets preserved unless --include-targets)
python -m target.cli clear-db   # sibling fingerprinter repo
```

Both already existed before this change and are unaffected by it — this
audit only removed the redundant, unsafe third path. Run each only when
that component's own fleet is stopped or between runs (each entrypoint's
`--clear-db --help` already says this); `docs/installation.md` documents
`main.py --clear-db`'s specific blast radius on `evidence:*` (a real prior
incident, 2026-09-02, unrelated to the bridge and not affected by this
change).

## 7. Tests

`tests/bridge_clear_db_removed_test.py` (new): both entrypoints reject
`--clear-db` with a normal argparse usage error (exit code 2, "unrecognized
arguments"), proving the flag is actually gone, not just undocumented.

`tests/redis_namespace_ownership_test.py` (new), against a real, isolated
local Redis (test db 1, this repo's existing convention): seeds both
`crawler`-namespace-style frontier keys/`evidence:*` keys and a synthetic
`fingerprint:*` key side by side in the same db, then proves
`RedisURLFrontier.clear()`/`RedisMediaEvidenceStore.clear()` (the crawler's
own authoritative reset, reached via `CrawlerManager.clear_storage()`)
never touches `fingerprint:*` — the crawler side of the ownership boundary
this whole audit is about. (The reverse direction —
`work_queue/admin.py::clear_all()` never touching `evidence:*`/frontier
keys — belongs in the fingerprinter repo's own test suite; this repo does
not import that code, per `bridge/fingerprint_stream_adapter.py`'s
module docstring.)
