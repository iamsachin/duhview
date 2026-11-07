"""Test PR only: per-model cost report for archived runs."""

import json
from pathlib import Path


def run_costs(runs_dir: Path) -> dict[str, float]:
    """Total cost per model across archived runs."""
    totals: dict[str, float] = {}
    for result in runs_dir.glob("*/result.json"):
        data = json.loads(result.read_text())
        totals[data["model"]] = totals.get(data["model"], 0.0) + data["cost_usd"]
    return totals


def format_costs(totals: dict[str, float]) -> str:
    """One line per model, most expensive first."""
    return "\n".join(f"{model}: ${cost:.4f}" for model, cost in sorted(totals.items(), key=lambda kv: -kv[1]))
