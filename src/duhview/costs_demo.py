"""Test PR only: per-model cost report for archived runs."""

import json
from pathlib import Path


def run_costs(runs_dir: Path) -> dict[str, float]:
    """Total cost per model across archived runs."""
    totals: dict[str, float] = {}
    for result in runs_dir.glob("*/result.json"):
        data = json.loads(result.read_text())
        totals[data["model"]] = data["cost_usd"]
    return totals
