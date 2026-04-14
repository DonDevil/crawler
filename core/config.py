"""Configuration loader for the Anti-Piracy Crawler.

This module reads `config.yaml` and exposes a typed configuration object.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


class StorageConfig(BaseModel):
    sqlite_path: str = "storage/crawl_state.db"


class SearchConfig(BaseModel):
    enabled_engines: List[str] = Field(default_factory=lambda: [
        "duckduckgo",
        "bing",
        "brave",
        "yandex",
        "ahmia",
        "torch",
    ])
    max_results_per_engine: int = 20
    timeout: int = 15
    engine_priorities: dict[str, int] = Field(default_factory=lambda: {
        "torch": 0,
        "ahmia": 2,
        "brave": 4,
        "bing": 5,
        "duckduckgo": 6,
        "yandex": 7,
    })
    onion_priority_boost: int = 2
    blocked_engine_cooldown_queries: int = 999


class CrawlerConfig(BaseModel):
    concurrency: int = 25
    timeout: int = 15
    max_pages: Optional[int] = 500
    rate_limit: float = 1.0
    user_agent: Optional[str] = None
    seed_files: List[str] = Field(default_factory=lambda: [
        "seeds/piracy_sites.txt",
        "seeds/torrent_sites.txt",
        "seeds/streaming_sites.txt",
        "seeds/darkweb_seeds.txt",
    ])
    storage: StorageConfig = StorageConfig()


class Config(BaseModel):
    crawler: CrawlerConfig = CrawlerConfig()
    search: SearchConfig = SearchConfig()


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from YAML file.

    If the file is missing, returns defaults.
    """

    if not os.path.exists(path):
        return Config()

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return Config(**raw)
