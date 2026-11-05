from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from duhview.types import Severity

CONFIG_PATH = ".duhview/config.yaml"
# Generated files: large, costly to send to a model, rarely worth reviewing.
DEFAULT_EXCLUDE = ["*.lock", "*package-lock.json", "*pnpm-lock.yaml", "*.min.js", "*.min.css", "*.map"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelCfg(_Strict):
    name: str = "claude-sonnet-5-5"
    verifier: str | None = None  # set to enable the verify pass


class ReviewCfg(_Strict):
    block_on_severity: Severity = Severity.major
    min_severity: Severity = Severity.minor
    exclude: list[str] = DEFAULT_EXCLUDE  # setting this replaces the defaults


class Rule(_Strict):
    text: str
    paths: list[str] = []


class BudgetCfg(_Strict):
    max_usd_per_review: float = 2.0
    max_tool_calls_per_agent: int = 40
    agent_timeout_s: int = 300


class Config(_Strict):
    model: ModelCfg = ModelCfg()
    review: ReviewCfg = ReviewCfg()
    rules: list[Rule] = []
    budget: BudgetCfg = BudgetCfg()
    # None = run whichever scanners are installed; a list = require exactly these.
    scanners: list[Literal["gitleaks", "semgrep"]] | None = None
    offline: bool = False  # skip scanners that need the network


def load_config(root: Path, read: Callable[[str], str | None] | None = None) -> Config:
    """`read` overrides where the file comes from, e.g. a PR's base commit."""
    if read is not None:
        text = read(CONFIG_PATH)
    else:
        path = root / CONFIG_PATH
        text = path.read_text() if path.exists() else None
    return Config.model_validate(yaml.safe_load(text) or {}) if text else Config()
