"""Manage a Tor process for onion routing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class TorManager:
    """Launch and stop a Tor process.

    This is a simple helper and does not manage Tor configuration.
    """

    def __init__(self, tor_path: str = "tor", data_dir: str = "tor/data"):
        self.tor_path = tor_path
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self.process is not None:
            return

        self.process = subprocess.Popen(
            [self.tor_path, "--DataDirectory", str(self.data_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        self.process.wait(timeout=10)
        self.process = None
