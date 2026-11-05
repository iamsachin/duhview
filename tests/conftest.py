import subprocess
from pathlib import Path

import pytest


def sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repo with one commit, an ignored .env and an uncommitted edit."""
    r = tmp_path / "repo"
    r.mkdir()
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "config", "user.email", "t@example.com")
    sh(r, "git", "config", "user.name", "t")
    (r / ".gitignore").write_text(".env\n")
    (r / ".env").write_text("SECRET=hunter2\n")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "README.md").write_text("hello\n")
    sh(r, "git", "add", ".")
    sh(r, "git", "commit", "-q", "-m", "init")
    return r.resolve()
