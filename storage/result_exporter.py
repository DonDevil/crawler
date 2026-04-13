"""Export crawl results to common formats."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Mapping


class ResultExporter:
    """Export results to CSV or JSON."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def to_json(self, filename: str, data: Mapping) -> Path:
        path = self.output_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return path

    def to_csv(self, filename: str, rows: Iterable[Mapping[str, str]]) -> Path:
        path = self.output_dir / filename
        rows = list(rows)
        if not rows:
            return path

        headers = list(rows[0].keys())
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

        return path
