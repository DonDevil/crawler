# `bridge.main` / `bridge.result_consumer_main --clear-db`

## Status: REMOVED (superseded)

**This feature was removed.** `bridge.main` and `bridge.result_consumer_main`
no longer accept `--clear-db` -- see
`docs/architecture/history/clear-db-ownership-audit.md` for the ownership
audit and the incident (a live fingerprinter worker crashing when a bridge
`--clear-db` deleted the `fingerprint:*` stream key it was blocked on
`XREADGROUP` against) that prompted it. The rest of this document is kept
as a historical record of what existed, below, unedited.

## Status (as of this feature's original addition): COMPLETE

## 1. Problem

`main.py --clear-db` already resets the crawler for a fresh run: it calls
`CrawlerManager.clear_storage()`, which clears the URL frontier
(`RedisURLFrontier.clear()` when Redis-backed) and the media evidence store
(`self.media_database.clear()`, `RedisMediaEvidenceStore.clear()` —
`evidence:*`). See
`docs/architecture/history/clear-db-redis-gap-audit.md` for the incident
that made this rigorous: `--clear-db` silently not reaching Redis let
stale, even schema-incompatible, keys from earlier runs "poison" a
supposedly-fresh one.

`bridge/main.py` and `bridge/result_consumer_main.py` are independently
deployable, long-running processes (own module docstrings: "Run as its own,
independently-deployable process") that read from and write into the same
`evidence:*` keyspace, and forward into / read results from the sibling
fingerprinter repo's `fingerprint:*` keyspace. Neither had a way to reset
state from its own entrypoint — an operator running the bridge processes
independently of the crawler's own CLI (a common case: they're meant to be
deployed separately, per the same docstring) had no equivalent of
`--clear-db` without going back to `main.py` or the fingerprinter's own
CLI.

## 2. What was added

`bridge/redis_reset.py` (new): a small, standalone SCAN+DELETE helper —

- `clear_namespace(redis_conn, namespace, *, exclude_prefixes=())` — the
  generic sweep, same pattern as `RedisURLFrontier.clear()` /
  `RedisMediaEvidenceStore.clear()` (SCAN + batched DELETE, never
  `KEYS`/`FLUSHDB`).
- `clear_fingerprint_namespace(redis_conn, *, include_targets=False)` —
  sweeps `fingerprint:*`, excluding `fingerprint:target:*` and
  `fingerprint:lock:*` by default (registered targets/embeddings are
  reference data an operator manages via the fingerprinter's own
  `target.cli`, not per-run state the bridge should ever touch — mirrors
  `fingerprinter/work_queue/admin.py::clear_all`'s exact same default).

This module deliberately does **not** import the fingerprinter repo.
`bridge/fingerprint_stream_adapter.py`'s own module docstring already
establishes why: a real Python import would pull in that repo's
torch/transformers virtualenv, and the two repos are meant to stay
independently deployable. So `clear_fingerprint_namespace()` is a small,
self-contained duplicate of `clear_all()`'s target/lock exclusion logic
(hand-kept in sync, same convention this bridge already uses for its other
"copied verbatim" constants — see `fingerprint_stream_adapter.py`,
`fingerprint_result_adapter.py`), not a call into that repo's code.

It is safe to sweep `fingerprint:*` from here at all only because the
bridge and the fingerprinter share one physical Redis instance/db by
construction — already documented in
`bridge/fingerprint_result_consumer.py::_blocking_read_client`'s docstring
("Same host/port/db/decoding as `store.redis_conn`... confirmed the same
physical Redis instance").

Both entrypoints gained a `--clear-db` flag, applied right after the
Redis-backend validation and before constructing the bridge/consumer:

```python
if args.clear_db:
    store.clear()                                        # evidence:*
    fp_deleted = clear_fingerprint_namespace(store.redis_conn)  # fingerprint:*
    logger.info(f"...: --clear-db cleared evidence state and {fp_deleted} fingerprint key(s)")
```

`store.clear()` already existed (`RedisMediaEvidenceStore.clear()`) — the
gap was only that neither bridge entrypoint called it, and neither had any
way to also reach the fingerprinter's namespace.

## 3. Usage

```
# One-shot fresh start, forward direction.
crawler/env/bin/python3 -m bridge.main --clear-db

# One-shot fresh start, result-consumer direction.
crawler/env/bin/python3 -m bridge.result_consumer_main --clear-db

# Combine with --once for a scripted smoke test / CI check.
crawler/env/bin/python3 -m bridge.main --clear-db --once
```

Run once, by hand, before starting the bridge/consumer fleet for a new run
— **never** as part of a supervised/auto-restart process command line
(systemd `ExecStart`, a container `CMD`, a process-group respawn policy). A
crash-restart with `--clear-db` baked into the launch command would wipe
every in-flight job on every restart, not just the very first one. Both
`--help` strings say this explicitly.

Either flag alone (bridge's or the result-consumer's) clears both
namespaces — `store.clear()` and `clear_fingerprint_namespace()` don't care
which process called them. Running both is redundant but harmless
(idempotent: clearing an already-empty namespace deletes 0 keys).

## 4. Full-pipeline reset

A complete fresh start across the whole crawler → bridge → fingerprinter
pipeline is three independent, prefix-scoped, order-independent calls:

1. `python main.py --clear-db` (this repo) — URL frontier + `evidence:*`.
2. `bridge.main --clear-db` or `bridge.result_consumer_main --clear-db`
   (this repo, this change) — `evidence:*` + `fingerprint:*` run state.
3. `python -m target.cli clear-db` (sibling fingerprinter repo) —
   `fingerprint:*` run state, with the same target/lock exclusion; see that
   repo's `docs/architecture/redis-full-reset-clear-db.md`.

Steps 1 and 2 overlap on `evidence:*`; steps 2 and 3 overlap on
`fingerprint:*`. That overlap is intentional — each entrypoint is usable
on its own, since crawler, bridge, and fingerprinter are deployed and
operated independently.

## 5. Tests

`tests/bridge_redis_reset_test.py` (new), run against real local Redis
(test DB 1, this repo's existing convention — see `bridge_test.py`), skips
cleanly if Redis is unavailable:

- `clear_namespace` deletes only the matching prefix, respects
  `exclude_prefixes`.
- `clear_fingerprint_namespace` preserves `target:*`/`lock:*` by default,
  deletes them with `include_targets=True`.

`tests/bridge_test.py` and `tests/bridge_stream_adapter_test.py` were
re-run unchanged to confirm the new import/argparse wiring in
`bridge/main.py` / `bridge/result_consumer_main.py` didn't regress
anything they cover.

**Measured, this session:**

```
python -m pytest tests/bridge_redis_reset_test.py -q
  -> 4 passed

python -m pytest tests/bridge_test.py tests/bridge_stream_adapter_test.py -q
  -> 40 passed, 1 skipped
```

`bridge.main --clear-db --once` and `bridge.result_consumer_main
--clear-db --once` were also smoke-tested end-to-end against a real local
Redis (seeded with `evidence:*`, `fingerprint:job:*`,
`fingerprint:results:stream:*`, and `fingerprint:target:*` keys): both
correctly deleted the evidence/job/result keys and left the target key
untouched.

## 6. Limitations

Same as the fingerprinter's own `clear-db` (see that repo's doc, §6): no
interactive confirmation prompt (matches `main.py --clear-db`'s existing,
immediate-on-flag behavior), and no local-filesystem cache cleanup (not
applicable here — the bridge has no local cache of its own).
