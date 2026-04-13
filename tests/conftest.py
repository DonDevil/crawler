"""Pytest configuration and path setup."""

import sys
from pathlib import Path

# Ensure the repository root is on sys.path so we can import local packages.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
