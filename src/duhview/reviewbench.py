"""Adapter for ReviewBench, an open benchmark of AI code reviewers on real pull requests.

The benchmark runs our container once per pull request with the checkout mounted at
/work/repo, the diff at $RB_DIFF and settings in RB_* variables, and reads the findings
we write to $RB_OUT. See integrations/reviewbench/ for the image.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from duhview.collect import Change, split_diff
from duhview.config import BudgetCfg, Config
from duhview.model import resolve_model
from duhview.review import run_review
from duhview.types import ReviewResult

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_FINDINGS = 50  # the contract warns that oversized output is a common failure


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"duhview: {name} is not set")
    return value


def findings_file(result: ReviewResult, nwo: str, pr_number: int, base: str, head: str, agent: str) -> dict[str, Any]:
    return {
        "pr": {"repo": f"https://github.com/{nwo}", "pr_number": pr_number, "base": base, "head": head},
        "agent": agent,
        "findings": [
            # The judge matches `message` against expert findings, so it carries title and explanation.
            {"file": f.file.removeprefix("./"), "start_line": f.start_line, "end_line": max(f.end_line, f.start_line),
             "message": f"{f.title}. {f.explanation}", "producer": agent}
            for f in result.findings[:MAX_FINDINGS]
        ],
    }


def main() -> int:
    repo = Path(os.environ.get("RB_REPO", "/work/repo")).resolve()  # RB_REPO only for local testing
    out = Path(_env("RB_OUT"))
    nwo, head, base = _env("RB_NWO"), _env("RB_HEAD"), _env("RB_BASE")
    pr_number, agent = int(_env("RB_PR_NUMBER")), _env("RB_AGENT", "duhview")
    model = os.environ.get("RB_CONFIG_MODEL", DEFAULT_MODEL)
    verifier = os.environ.get("RB_CONFIG_VERIFIER") or None
    budget = float(os.environ.get("RB_CONFIG_BUDGET", "2.0"))
    # The contract asks agents to print the settings they apply.
    print(f"duhview: model={model} verifier={verifier or 'off'} budget={budget}", file=sys.stderr)

    # Their diff is exactly what GitHub shows. Scanners are off: the image has none, and the
    # network only reaches the model API.
    change = Change(f"{nwo}#{pr_number}", split_diff(Path(_env("RB_DIFF")).read_text(errors="replace")))
    cfg = Config(scanners=[], budget=BudgetCfg(max_usd_per_review=budget))
    finder = resolve_model(model)
    checker = resolve_model(verifier) if verifier else None
    archive = Path(tempfile.mkdtemp(prefix="duhview-"))  # keep the mounted repo untouched
    result = asyncio.run(run_review(repo, change, cfg, finder, checker, None, archive_root=archive))

    for line in result.incomplete:
        print(f"duhview: incomplete: {line}", file=sys.stderr)
    if result.incomplete and not result.findings:
        # Writing an empty list would claim "reviewed, found nothing". Fail so the run retries.
        print("duhview: review failed; not writing findings", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings_file(result, nwo, pr_number, base, head, agent), indent=2))
    print(f"duhview: {len(result.findings)} finding(s), ${result.cost_usd:.4f} -> {out}", file=sys.stderr)
    return 0
