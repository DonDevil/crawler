"""Fingerprint-result consumer process entrypoint: consumes the
fingerprinter's `fingerprint:results:stream:{priority}` and completes the
corresponding crawler-side forwarded job with the terminal verdict.

Run as its own, independently-deployable process -- not part of the crawl
worker process, not part of the bridge's forward-direction process, not part
of the fingerprint worker process (mirrors `bridge/main.py`'s own framing):

    crawler/env/bin/python3 -m bridge.result_consumer_main

See docs/architecture/phase-4-crawler-fingerprinter-bridge.md for the
forward-direction design this mirrors in reverse.
"""
from __future__ import annotations

import argparse
import signal
from types import FrameType
from typing import Optional

from loguru import logger

from bridge.fingerprint_result_consumer import FingerprintResultConsumer
from bridge.redis_reset import clear_fingerprint_namespace
from core.config import load_config
from core.crawler_manager import build_media_evidence_store
from storage.redis_media_evidence_store import RedisMediaEvidenceStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Fingerprinter -> crawler evidence result consumer")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: %(default)s).")
    parser.add_argument("--consumer-name", default="result-consumer-1", help="Consumer identity for this process.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one result batch then exit, instead of running forever. For "
        "testing/debugging/one-shot invocations.",
    )
    parser.add_argument(
        "--clear-db",
        action="store_true",
        help="Clear all evidence-job state (this store's 'evidence:*' keys) and fingerprinter "
        "run state ('fingerprint:*' keys -- jobs/results/retries/matches/submission markers, "
        "never registered targets) before starting, for a fresh run with no state carried over "
        "from a previous one. Run once, by hand, before starting the consumer fleet -- never as "
        "part of a supervised/auto-restart command line, since that would wipe in-flight jobs "
        "on every crash-restart.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    store = build_media_evidence_store(config)
    if store is None:
        parser.error("Media evidence is disabled (crawler.storage.enable_media_evidence: false)")
    if not isinstance(store, RedisMediaEvidenceStore):
        parser.error(
            "The result consumer requires the Redis media evidence backend -- the fingerprinter's "
            f"result stream it reads only exists in Redis. Got media_evidence.type="
            f"{config.crawler.media_evidence.type!r}."
        )

    if args.clear_db:
        store.clear()
        fp_deleted = clear_fingerprint_namespace(store.redis_conn)
        logger.info(f"result-consumer: --clear-db cleared evidence state and {fp_deleted} fingerprint key(s)")

    rc_config = config.crawler.result_consumer
    consumer = FingerprintResultConsumer(
        store,
        consumer_name=args.consumer_name,
        priorities=rc_config.priorities,
        consumer_group=rc_config.consumer_group,
        block_ms=rc_config.block_ms,
        lease_ms=rc_config.lease_ms,
        reclaim_batch_size=rc_config.reclaim_batch_size,
    )

    def _handle_signal(signum: int, frame: Optional[FrameType]) -> None:
        logger.info(f"result-consumer: received signal {signum}, shutting down gracefully")
        consumer.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        if args.once:
            got_result = consumer.process_one()
            if not got_result:
                logger.info("result-consumer: --once found no results, nothing to do")
        else:
            logger.info(f"result-consumer: starting consumer_name={args.consumer_name!r}")
            consumer.run_forever(
                reclaim_interval_seconds=rc_config.reclaim_interval_seconds,
                idle_sleep_seconds=rc_config.idle_sleep_seconds,
            )
    finally:
        logger.info(f"result-consumer: shutdown metrics={consumer.metrics}")
        consumer.close()
        store.close()


if __name__ == "__main__":
    main()
