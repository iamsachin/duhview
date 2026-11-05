from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from duhview.types import Severity

CONFIG_PATH = ".duhview/config.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelCfg(_Strict):
    name: str = "claude-sonnet-5-5"
    verifier: str | None = None


class ReviewCfg(_Strict):
    block_on_severity: Severity = Severity.major
    min_severity: Severity = Severity.minor
    exclude: list[str] = []


class Rule(_Strict):
    text: str
    paths: list[str] = []


class BudgetCfg(_Strict):
    max_usd_per_review: float = 1.0
    max_tool_calls_per_agent: int = 40
    agent_timeout_s: int = 300


class Config(_Strict):
    model: ModelCfg = ModelCfg()
    review: ReviewCfg = ReviewCfg()
    rules: list[Rule] = []
    budget: BudgetCfg = BudgetCfg()


def load_config(root: Path) -> Config:
    path = root / CONFIG_PATH
    if not path.exists():
        return Config()
    return Config.model_validate(yaml.safe_load(path.read_text()) or {})
