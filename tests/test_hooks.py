import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from duhview import hooks, model
from duhview.cli import app

from .test_core import _resp


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r.stdout.strip() + r.stderr.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def pushed(repo: Path, tmp_path: Path) -> Path:
    """`repo` with a bare origin that already has main."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    return repo


def _line(local: str, remote_sha: str, ref: str = "refs/heads/main") -> str:
    return f"{ref} {local} {ref} {remote_sha}\n"


# --- which commits a push adds ------------------------------------------------------------------


def test_normal_push_reviews_what_the_remote_lacks(pushed: Path) -> None:
    remote_head = _git(pushed, "rev-parse", "HEAD")
    head = _commit(pushed, "a.py", "x = 1\n")
    (r,) = hooks.push_ranges(pushed, "origin", _line(head, remote_head))
    assert (r.base, r.head) == (remote_head, head)


def test_new_branch_reviews_only_commits_no_remote_branch_has(pushed: Path) -> None:
    main = _git(pushed, "rev-parse", "HEAD")
    _git(pushed, "switch", "-q", "-c", "feature")
    _commit(pushed, "a.py", "1\n")
    head = _commit(pushed, "b.py", "2\n")
    (r,) = hooks.push_ranges(pushed, "origin", _line(head, hooks.ZERO, "refs/heads/feature"))
    assert (r.base, r.head) == (main, head)


def test_force_push_diffs_from_where_histories_meet(pushed: Path) -> None:
    base = _git(pushed, "rev-parse", "HEAD")
    old = _commit(pushed, "a.py", "old\n")
    _git(pushed, "push", "-q", "origin", "main")
    _git(pushed, "reset", "-q", "--hard", base)
    new = _commit(pushed, "a.py", "new\n")
    (r,) = hooks.push_ranges(pushed, "origin", _line(new, old))
    assert r.base == base


def test_deletions_and_nothing_new_are_skipped(pushed: Path) -> None:
    head = _git(pushed, "rev-parse", "HEAD")
    stdin = f"(delete) {hooks.ZERO} refs/heads/old {head}\n" + _line(head, hooks.ZERO, "refs/heads/same")
    assert hooks.push_ranges(pushed, "origin", stdin) == []


def test_first_push_of_a_root_commit_diffs_against_the_empty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    head = _commit(repo, "a.py", "x = 1\n")
    (r,) = hooks.push_ranges(repo, "origin", _line(head, hooks.ZERO))
    assert r.base == hooks.EMPTY_TREE


# --- installing ------------------------------------------------------------------------------


def test_install_refuses_a_foreign_hook_unless_forced(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(ValueError, match="Add this line"):
        hooks.install_hook(repo, force=False)
    assert hook.read_text() == "#!/bin/sh\necho mine\n"
    hooks.install_hook(repo, force=True)
    assert hooks.HOOK_MARKER in hook.read_text()
    assert (hook.parent / "pre-push.bak").read_text() == "#!/bin/sh\necho mine\n"
    hooks.install_hook(repo, force=False)  # reinstalling over our own hook is fine
    assert os.access(hook, os.X_OK)


def test_install_respects_core_hooks_path(repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", ".githooks")
    assert hooks.install_hook(repo, force=False) == str(repo / ".githooks" / "pre-push")


# --- running the hook ------------------------------------------------------------------------


def _fake_model(monkeypatch: pytest.MonkeyPatch, severity: str) -> list[str]:
    seen: list[str] = []

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        seen.append(messages[1]["content"])
        return _resp(content=json.dumps({"findings": [{
            "file": "a.py", "start_line": 1, "end_line": 1, "severity": severity, "category": "bug",
            "title": "t", "explanation": "e"}]}))

    monkeypatch.setattr(model, "complete", fake_complete)
    return seen


@pytest.fixture
def push_stdin(pushed: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    remote_head = _git(pushed, "rev-parse", "HEAD")
    head = _commit(pushed, "a.py", "x = eval(input())\n")
    (pushed / "README.md").write_text("uncommitted, must not be reviewed or touched\n")
    monkeypatch.chdir(pushed)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("DUHVIEW", raising=False)
    return _line(head, remote_head)


def test_hook_blocks_on_serious_findings_and_reviews_only_pushed_commits(
        pushed: Path, push_stdin: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_model(monkeypatch, "major")
    out = CliRunner().invoke(app, ["hook-run", "origin", "url"], input=push_stdin)
    assert out.exit_code == 1 and "push blocked" in out.output and "DUHVIEW=skip" in out.output
    assert "+x = eval(input())" in seen[0] and "uncommitted" not in seen[0]
    assert (pushed / "README.md").read_text().startswith("uncommitted")


def test_hook_lets_minor_findings_through(push_stdin: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_model(monkeypatch, "minor")
    assert CliRunner().invoke(app, ["hook-run", "origin", "url"], input=push_stdin).exit_code == 0


def test_hook_never_strands_you_without_an_api_key(push_stdin: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    out = CliRunner().invoke(app, ["hook-run", "origin", "url"], input=push_stdin)
    assert out.exit_code == 0 and "not reviewed" in out.output


@pytest.mark.parametrize("mode", ["skip", "vouch"])
def test_bypass_is_allowed_but_recorded(pushed: Path, push_stdin: str, monkeypatch: pytest.MonkeyPatch,
                                        mode: str) -> None:
    seen = _fake_model(monkeypatch, "critical")
    monkeypatch.setenv("DUHVIEW", mode)
    out = CliRunner().invoke(app, ["hook-run", "origin", "url"], input=push_stdin)
    assert out.exit_code == 0 and f"{mode} recorded" in out.output and seen == []
    (record,) = (pushed / ".duhview" / "runs").glob("*/bypass.json")
    data = json.loads(record.read_text())
    assert data["mode"] == mode and data["by"] == "t@example.com" and data["pushed"][0]["ref"] == "refs/heads/main"


# --- the real thing: git push through the installed hook (no model calls) --------------------


def test_real_git_push_runs_the_installed_hook(pushed: Path) -> None:
    hooks.install_hook(pushed, force=False)
    _commit(pushed, "a.py", "x = 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DUHVIEW")}
    out = _git(pushed, "push", "origin", "main", env=env)  # no key: warns, never blocks
    assert "duhview: refs/heads/main not reviewed" in out
    _commit(pushed, "b.py", "y = 2\n")
    out = _git(pushed, "push", "origin", "main", env=env | {"DUHVIEW": "skip"})
    assert "duhview: skip recorded" in out
    assert _git(pushed, "rev-parse", "HEAD") == _git(pushed, "rev-parse", "origin/main")


def test_install_skill(tmp_path: Path) -> None:
    out = CliRunner().invoke(app, ["install-skill", "--path", str(tmp_path)])
    assert out.exit_code == 0, out.output
    text = (tmp_path / "duhview" / "SKILL.md").read_text()
    assert text.startswith("---\nname: duhview\n") and "duhview review --json" in text
