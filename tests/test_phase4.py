import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from duhview import model
from duhview.collect import (
    MAX_FILE_DIFF_CHARS,
    UNIT_MAX_CHARS,
    Change,
    Target,
    added_lines,
    get_change,
    review_units,
)
from duhview.config import Config
from duhview.report import to_sarif
from duhview.review import run_review
from duhview.scan import run_scanners
from duhview.types import FinderOutput, Finding, FindingDraft, ReviewResult

from .test_core import _resp

# --- diff parsing -----------------------------------------------------------------------------

DIFF = """diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -1,4 +1,5 @@
 keep
-old
+new1
+++not a header inside a hunk
 keep
@@ -10,2 +11,3 @@ def g():
 ctx
+added
\\ No newline at end of file
"""


def test_added_lines_tracks_hunks_and_ignores_markers() -> None:
    assert added_lines(DIFF) == {2, 3, 12}


def test_review_units_pack_in_path_order_and_respect_cap() -> None:
    big = "x" * (UNIT_MAX_CHARS - 10)
    change = Change("t", {"b/2.py": "y" * 20, "a/1.py": big, "b/1.py": "z" * 20})
    units = review_units(change)
    assert [list(u) for u in units] == [["a/1.py"], ["b/1.py", "b/2.py"]]


def test_huge_file_diff_is_truncated_and_flagged(repo: Path) -> None:
    (repo / "src" / "app.py").write_text("x = 1\n" * (MAX_FILE_DIFF_CHARS // 4))
    change = get_change(repo, Target(), Config())
    assert change.truncated == ["src/app.py"]
    assert len(change.files["src/app.py"]) < MAX_FILE_DIFF_CHARS + 100


def test_lockfiles_excluded_by_default(repo: Path) -> None:
    (repo / "pnpm-lock.yaml").write_text("lock\n")
    (repo / "Cargo.lock").write_text("lock\n")
    (repo / "src" / "app.py").write_text("x = 2\n")
    assert list(get_change(repo, Target(), Config()).files) == ["src/app.py"]


# --- scanners (fake executables on PATH) -----------------------------------------------------

FAKE_GITLEAKS = """#!/bin/sh
line=$(grep -n 'AKIA' | head -1 | cut -d: -f1)
if [ -n "$line" ]; then
  printf '[{"RuleID":"aws-access-token","Description":"AWS key","StartLine":%s,"EndLine":%s}]' "$line" "$line"
else
  printf '[]'
fi
"""

FAKE_SEMGREP = """#!/bin/sh
printf '{"results":[{"check_id":"python.lang.security.eval","path":"src/app.py","start":{"line":1},
"end":{"line":1},"extra":{"message":"eval of untrusted input","severity":"ERROR",
"metadata":{"category":"security"}}},{"check_id":"python.old","path":"src/app.py","start":{"line":9},
"end":{"line":9},"extra":{"message":"pre-existing","severity":"WARNING"}}],"errors":[]}'
exit 1
"""


def _install(bin_dir: Path, name: str, script: str) -> None:
    p = bin_dir / name
    p.write_text(script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", f"{d}:/usr/bin:/bin")
    return d


def _change_with_secret(repo: Path) -> Change:
    (repo / "src" / "app.py").write_text('KEY = "AKIAABCDEFGHIJKLMNOP"\ndef add(a, b):\n    return a + b\n')
    return get_change(repo, Target(), Config())


def test_scanner_findings_parsed_and_limited_to_added_lines(repo: Path, fake_bin: Path) -> None:
    _install(fake_bin, "gitleaks", FAKE_GITLEAKS)
    _install(fake_bin, "semgrep", FAKE_SEMGREP)
    out = run_scanners(repo, _change_with_secret(repo), Config(), offline=False)
    assert [(n, d.start_line, d.severity, d.category) for n, d in out.findings] == [
        ("gitleaks", 1, "critical", "security"),
        ("semgrep", 1, "major", "security"),  # the line-9 result is not in the change, so dropped
    ]
    assert "AKIA" not in json.dumps([d.model_dump() for _, d in out.findings])
    assert not out.notes and not out.incomplete


def test_missing_optional_scanner_is_a_note_but_required_one_is_incomplete(repo: Path, fake_bin: Path) -> None:
    change = _change_with_secret(repo)
    auto = run_scanners(repo, change, Config(), offline=False)
    assert auto.incomplete == [] and len(auto.notes) == 2
    required = run_scanners(repo, change, Config(scanners=["gitleaks"]), offline=False)
    assert required.incomplete == ["gitleaks is not installed; skipped"]


def test_offline_skips_registry_semgrep(repo: Path, fake_bin: Path) -> None:
    _install(fake_bin, "semgrep", FAKE_SEMGREP)
    out = run_scanners(repo, _change_with_secret(repo), Config(scanners=["semgrep"]), offline=True)
    assert out.findings == [] and "offline" in out.notes[0] and not out.incomplete


def test_scanner_crash_makes_review_incomplete(repo: Path, fake_bin: Path) -> None:
    _install(fake_bin, "gitleaks", "#!/bin/sh\necho boom >&2\nexit 7\n")
    out = run_scanners(repo, _change_with_secret(repo), Config(scanners=["gitleaks"]), offline=False)
    assert out.incomplete and "boom" in out.incomplete[0]


# --- pipeline: units in parallel + scanner merge ---------------------------------------------


def test_each_unit_gets_its_own_finder_and_scanner_findings_skip_verify(
        repo: Path, fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(fake_bin, "gitleaks", FAKE_GITLEAKS)
    (repo / "src" / "big.py").write_text("y = 1\n" * (UNIT_MAX_CHARS // 5))  # forces a second unit
    change = _change_with_secret(repo)
    finder_inputs: list[str] = []

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        if "verifier" in messages[0]["content"][:60]:
            return _resp(content=json.dumps({"verdict": "confirm", "severity": "minor", "confidence": 0.8,
                                             "reason": "r"}))
        finder_inputs.append(messages[1]["content"])
        found = [{"file": "src/app.py", "start_line": 3, "end_line": 3, "severity": "major", "category": "bug",
                  "title": "agent finding", "explanation": "e"}] if "+++ b/src/app.py" in messages[1]["content"] else []
        return _resp(content=json.dumps({"findings": found}))

    monkeypatch.setattr(model, "complete", fake_complete)
    result = asyncio.run(run_review(repo, change, Config(), "m", "m", None))
    assert result.units == 2 and len(finder_inputs) == 2
    app_unit = next(i for i in finder_inputs if "+++ b/src/app.py" in i)
    assert "<scanner_results>" in app_unit and "src/big.py" in app_unit  # leads + other files listed
    by_source = {f.source: f for f in result.findings}
    assert by_source["scanner:gitleaks"].verified is False  # deterministic, not sent to verifier
    assert by_source["agent"].verified is True and by_source["agent"].severity == "minor"
    assert "semgrep is not installed; skipped" in result.notes


def test_agent_winds_down_near_its_budget_share_and_still_answers(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent that would explore forever must stop near its share and return what it found."""
    from duhview import agent
    from duhview.tools import RepoTools

    turns: list[str] = []

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        last = messages[-1]
        if last["role"] == "tool" and "limit reached" in last["content"]:
            turns.append("answer")
            return _resp(content='{"findings": []}')
        turns.append("tool")
        return _resp(tool=("list_dir", "{}"))

    monkeypatch.setattr(model, "complete", fake_complete)
    monkeypatch.setattr(model, "cost_of", lambda _: 0.1)
    out = asyncio.run(agent.ask_json("m", "s", "u", FinderOutput, RepoTools(repo), model.Budget(10), 40, [],
                                     max_usd=0.5))
    assert out.findings == []
    # 0.1 per turn, reserve 3 turns: winds down once 0.2 + 0.3 >= 0.5, i.e. after the 2nd turn.
    assert turns == ["tool", "tool", "answer"]


# --- SARIF ----------------------------------------------------------------------------------


def test_sarif_structure() -> None:
    f = Finding.from_draft(FindingDraft(file="a.py", start_line=3, end_line=3, severity="critical",
                                        category="security", title="t", explanation="e"), source="scanner:gitleaks")
    r = ReviewResult(target="t", model="m", verifier_model=None, findings=[f], incomplete=[], cost_usd=0,
                     cost_known=True)
    run = to_sarif(r)["runs"][0]
    assert run["tool"]["driver"]["rules"][0]["id"] == "scanner/gitleaks/security"
    res = run["results"][0]
    assert res["level"] == "error" and res["ruleIndex"] == 0
    assert res["locations"][0]["physicalLocation"]["region"] == {"startLine": 3, "endLine": 3}
    assert res["partialFingerprints"]["duhview/v1"] == f.id
