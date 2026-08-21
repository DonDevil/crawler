"""Bridge process entrypoint (Phase 4): forwards crawler evidence jobs
(`evidence:jobs:queue`) onto the fingerprinter's own
`fingerprint:jobs:stream:{priority}` contract.

Run as its own, independently-deployable process -- not part of the crawl
worker process, not part of the fingerprint worker process:

    crawler/env/bin/python3 -m bridge.main

See docs/architecture/phase-4-crawler-fingerprinter-bridge.md for the full
design, configuration reference, and validation results.
"""
from __future__ import annotations

import argparse
import signal
from types import FrameType
from typing import Optional

from loguru import logger

from bridge.crawler_fingerprinter_bridge import BridgeRuntimeConfig, CrawlerFingerprinterBridge
from core.config import load_config
from core.crawler_manager import build_media_evidence_store
from storage.redis_media_evidence_store import RedisMediaEvidenceStore


def _runtime_config_from(bridge_cfg, media_evidence_cfg) -> BridgeRuntimeConfig:
    return BridgeRuntimeConfig(
        max_outstanding_jobs=bridge_cfg.max_outstanding_jobs,
        submission_marker_ttl_s=bridge_cfg.submission_marker_ttl_s,
        priority_high_max=bridge_cfg.priority_high_max,
        priority_low_min=bridge_cfg.priority_low_min,
        max_attempts=bridge_cfg.max_attempts,
        idle_sleep_seconds=bridge_cfg.poll_idle_sleep_seconds,
        reclaim_interval_seconds=bridge_cfg.reclaim_interval_seconds,
        reclaim_batch_size=media_evidence_cfg.reclaim_batch_size,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawler -> fingerprinter evidence bridge (Phase 4)")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: %(default)s).")
    parser.add_argument("--worker-name", default="bridge-1", help="Claim identity for this bridge process.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one evidence job then exit, instead of running forever. For "
        "testing/debugging/one-shot invocations.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    store = build_media_evidence_store(config)
    if store is None:
        parser.error("Media evidence is disabled (crawler.storage.enable_media_evidence: false)")
    if not isinstance(store, RedisMediaEvidenceStore):
        parser.error(
            "The bridge requires the Redis media evidence backend -- target scoping (and therefore "
            f"any job the bridge can legally forward) only exists there. Got media_evidence.type="
            f"{config.crawler.media_evidence.type!r}."
        )

    bridge = CrawlerFingerprinterBridge(
        store,
        worker_id=args.worker_name,
        config=_runtime_config_from(config.crawler.bridge, config.crawler.media_evidence),
    )

    def _handle_signal(signum: int, frame: Optional[FrameType]) -> None:
        logger.info(f"bridge: received signal {signum}, shutting down gracefully")
        bridge.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        if args.once:
            got_job = bridge.process_one()
            if not got_job:
                logger.info("bridge: --once found an empty queue, nothing to do")
        else:
            logger.info(f"bridge: starting worker_id={args.worker_name!r}")
            bridge.run_forever()
    finally:
        logger.info(f"bridge: shutdown metrics={bridge.metrics}")
        store.close()


if __name__ == "__main__":
    main()
