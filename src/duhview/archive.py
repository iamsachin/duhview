import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNS_DIR = ".duhview/runs"


class Archive:
    """Stores each run's prompts, raw outputs and results under .duhview/runs/<timestamp>/."""

    def __init__(self, root: Path):
        runs = root / RUNS_DIR
        runs.mkdir(parents=True, exist_ok=True)
        ignore = runs / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")  # runs contain source code and model output; never commit them
        self.dir = runs / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.dir.mkdir()

    def write(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2, default=str))
        return path
