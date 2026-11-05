import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from duhview import bench, model
from duhview.collect import Target, get_change, project_context
from duhview.config import Config
from duhview.review import run_review
from duhview.types import Finding, FindingDraft

from .test_core import _resp

# --- benchmark cases --------------------------------------------------------------------------


def test_every_case_loads_and_each_location_is_unique() -> None:
    cases = bench.load_cases()
    assert len(cases) >= 8
    assert sum(len(c.spec.bugs) for c in cases) >= 17
    assert any(not c.spec.bugs for c in cases), "need clean cases to measure false positives"


def test_unknown_case_name_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown case"):
        bench.load_cases(["nope"])


def test_build_repo_leaves_after_tree_as_uncommitted_change(tmp_path: Path) -> None:
    (case,) = bench.load_cases(["py_cross_file"])
    bench.build_repo(case, tmp_path)
    change = get_change(tmp_path.resolve(), Target(), Config())
    assert set(change.files) == {"money.py", "thumbs.py"}  # orders.py is unchanged


def _finding(file: str, line: int) -> Finding:
    return Finding.from_draft(FindingDraft(file=file, start_line=line, end_line=line, severity="major",
                                           category="bug", title="t", explanation="e"))


def test_score_matches_by_file_and_nearby_line() -> None:
    (case,) = bench.load_cases(["py_cross_file"])
    caller_line = case.lines["caller-not-updated"][0][1]
    caught, true = bench.score(case, [
        _finding("orders.py", caller_line + 2),  # within tolerance -> hit
        _finding("orders.py", caller_line + 20),  # too far -> false positive
        _finding("other.py", caller_line),  # wrong file -> false positive
    ])
    assert caught == ["caller-not-updated"] and true == 1


def test_totals() -> None:
    s = [bench.Score("a", 2, ["x"], 3, 1, 0.1, []), bench.Score("b", 0, [], 1, 0, 0.2, [])]
    assert bench.totals(s) == {"recall": 0.5, "precision": 0.25, "false_positives": 3, "cost_usd": 0.3}


# --- false-positive filter / context ----------------------------------------------------------


def test_fp_filter_is_included_in_context(repo: Path) -> None:
    (repo / ".duhview").mkdir()
    (repo / ".duhview" / "fp-filter.md").write_text("Missing docstrings.")
    assert "Do not report" in project_context(repo, Config(), [], None)
    assert "Missing docstrings." in project_context(repo, Config(), [], None)


def test_context_file_symlinked_outside_repo_is_ignored(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("top secret")
    os.symlink(tmp_path / "secret.txt", repo / "AGENTS.md")
    assert "top secret" not in project_context(repo, Config(), [], None)


# --- pipeline with a scripted model ---------------------------------------------------------


@pytest.mark.parametrize(("verifier", "expected"), [("m", ["kept"]), (None, ["kept", "dropped"])])
def test_verifier_drops_rejected_findings(repo: Path, monkeypatch: pytest.MonkeyPatch,
                                          verifier: str | None, expected: list[str]) -> None:
    drafts = [{"file": "src/app.py", "start_line": 1, "end_line": 1, "severity": "major", "category": "bug",
               "title": t, "explanation": "e"} for t in ("kept", "dropped")]
    drafts[1]["start_line"] = drafts[1]["end_line"] = 2
    drafts[1]["category"] = "security"  # avoid dedupe

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        if "verifier" not in messages[0]["content"][:60]:
            return _resp(content=json.dumps({"findings": drafts}))
        verdict = "reject" if '"dropped"' in messages[1]["content"] else "confirm"
        return _resp(content=json.dumps({"verdict": verdict, "severity": "major", "confidence": 0.9, "reason": "r"}))

    monkeypatch.setattr(model, "complete", fake_complete)
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    cfg = Config(scanners=[])
    result = asyncio.run(run_review(repo, get_change(repo, Target(), cfg), cfg, "m", verifier, None))
    assert sorted(f.title for f in result.findings) == sorted(expected)
    assert all(f.verified == (verifier is not None) for f in result.findings)
    assert not result.incomplete
