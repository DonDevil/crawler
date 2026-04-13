"""Logging configuration for the crawler.

This module configures `loguru` with a sane default formatter and level.
"""

from __future__ import annotations

import sys

from loguru import logger


def configure_logging(level: str = "INFO") -> None:
    """Configure the Loguru logger.

    This is safe to call multiple times.
    """

    # Avoid configuring logger multiple times when imported multiple times.
    logger.remove()
    logger.add(
        sink=sys.stdout,
        level=level,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )
