import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from duhview import model
from duhview.cli import app

from .conftest import sh
from .test_core import _resp

BASE_HEAD: dict[str, str] = {}


@pytest.fixture
def task(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout at head plus the contract's inputs, like the benchmark mounts them."""
    base = subprocess_out(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    sh(repo, "git", "commit", "-qam", "change")
    head = subprocess_out(repo, "rev-parse", "HEAD")
    (tmp_path / "diff.patch").write_text(subprocess_out(repo, "diff", f"{base}...{head}") + "\n")
    for k, v in {"RB_REPO": str(repo), "RB_OUT": str(tmp_path / "out" / "findings.json"), "RB_NWO": "acme/web",
                 "RB_PR_NUMBER": "1938", "RB_BASE": base, "RB_HEAD": head, "RB_AGENT": "duhview-test",
                 "RB_DIFF": str(tmp_path / "diff.patch"), "RB_CONFIG_MODEL": "gpt-5.5", "OPENAI_API_KEY": "x"}.items():
        monkeypatch.setenv(k, v)
    BASE_HEAD.update(base=base, head=head)
    return tmp_path / "out" / "findings.json"


def subprocess_out(repo: Path, *args: str) -> str:
    import subprocess
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _model_says(monkeypatch: pytest.MonkeyPatch, findings: list[dict[str, Any]] | None) -> list[str]:
    seen: list[str] = []

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        seen.append(m)
        if findings is None:
            raise RuntimeError("model down")
        return _resp(content=json.dumps({"findings": findings}))

    monkeypatch.setattr(model, "complete", fake_complete)
    return seen


def test_writes_the_contracts_findings_file(task: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    seen = _model_says(monkeypatch, [{"file": "src/app.py", "start_line": 2, "end_line": 2, "severity": "major",
                                      "category": "bug", "title": "Subtracts instead of adds",
                                      "explanation": "add(1, 2) returns -1."}])
    out = CliRunner().invoke(app, ["reviewbench"])
    assert out.exit_code == 0, out.output
    assert "model=gpt-5.5" in out.output and seen[0] == "openai/gpt-5.5"  # settings are read and printed
    data = json.loads(task.read_text())
    assert data["pr"] == {"repo": "https://github.com/acme/web", "pr_number": 1938, "base": BASE_HEAD["base"],
                          "head": BASE_HEAD["head"]}
    assert data["agent"] == "duhview-test"
    assert data["findings"] == [{"file": "src/app.py", "start_line": 2, "end_line": 2,
                                 "message": "Subtracts instead of adds. add(1, 2) returns -1.",
                                 "producer": "duhview-test"}]
    assert not (repo / ".duhview").exists()  # the mounted repo is left untouched


def test_nothing_found_is_an_empty_list(task: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _model_says(monkeypatch, [])
    assert CliRunner().invoke(app, ["reviewbench"]).exit_code == 0
    assert json.loads(task.read_text())["findings"] == []


def test_a_failed_review_exits_nonzero_instead_of_claiming_clean(task: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _model_says(monkeypatch, None)
    out = CliRunner().invoke(app, ["reviewbench"])
    assert out.exit_code == 1 and "review failed" in out.output and not task.exists()
