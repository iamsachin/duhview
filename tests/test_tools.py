import asyncio
import os
from pathlib import Path

import pytest

from duhview.tools import RepoTools, ToolError


def call(t: RepoTools, name: str, args: str) -> str:
    return asyncio.run(t.call(name, args))


def test_reads_tracked_file_with_line_numbers(repo: Path) -> None:
    assert RepoTools(repo).read_file("src/app.py") == "1\tdef add(a, b):\n2\t    return a + b"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "src/../../x"])
def test_rejects_paths_outside_repo(repo: Path, path: str) -> None:
    with pytest.raises(ToolError):
        RepoTools(repo).read_file(path)


def test_rejects_symlink_escaping_repo(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("top secret")
    os.symlink(tmp_path / "secret.txt", repo / "link.txt")
    with pytest.raises(ToolError, match="outside"):
        RepoTools(repo).read_file("link.txt")


def test_hides_gitignored_files(repo: Path) -> None:
    t = RepoTools(repo)
    with pytest.raises(ToolError, match="ignored"):
        t.read_file(".env")
    assert "hunter2" not in t.grep("hunter2")
    assert ".env" not in t.list_dir(".")


def test_sees_untracked_not_ignored_files(repo: Path) -> None:
    (repo / "new.py").write_text("x = 1\n")
    assert "x = 1" in RepoTools(repo).read_file("new.py")


def test_binary_file_refused(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ToolError, match="binary"):
        RepoTools(repo).read_file("blob.bin")


def test_large_file_read_is_capped(repo: Path) -> None:
    (repo / "big.txt").write_text("line\n" * 5000)
    out = RepoTools(repo).read_file("big.txt")
    assert "1500\tline" in out and "1501\t" not in out and "5000 lines total" in out


def test_grep_literal_by_default_and_regex_opt_in(repo: Path) -> None:
    t = RepoTools(repo)
    assert "src/app.py:2:" in t.grep("a + b")
    assert "src/app.py:1:" in t.grep(r"def \w+", regex=True)


def test_list_dir(repo: Path) -> None:
    assert RepoTools(repo).list_dir(".").splitlines() == [".gitignore", "README.md", "src/"]


def test_call_reports_errors_instead_of_raising(repo: Path) -> None:
    t = RepoTools(repo)
    assert call(t, "read_file", '{"path": "../x"}').startswith("error:")
    assert call(t, "nope", "{}").startswith("error: unknown tool")
    assert call(t, "read_file", '{"bad_arg": 1}').startswith("error:")
    assert call(t, "grep", '{"pattern": "(", "regex": true}').startswith("error:")


def test_pathological_regex_is_killed_not_left_running(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from duhview import tools

    (repo / "slow.txt").write_text("a" * 40 + "!\n")
    monkeypatch.setattr(tools, "TOOL_TIMEOUT_S", 1)
    start = time.monotonic()
    out = call(RepoTools(repo), "grep", '{"pattern": "(a+)+$", "regex": true}')
    assert out.startswith("error: regex search timed out") and time.monotonic() - start < 5
