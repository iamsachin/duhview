"""Collect what to review: the diff, changed files and project context."""

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from duhview.config import Config

CONTEXT_FILES = ["AGENTS.md", "CLAUDE.md"]  # read from the code under review
RULES_FILE = ".duhview/rules.md"  # trusted: in PR mode read from the base branch
FP_FILTER_FILE = ".duhview/fp-filter.md"  # trusted: in PR mode read from the base branch

Reader = Callable[[str], str | None]  # repo-relative path -> contents, or None if missing
MAX_CONTEXT_CHARS = 20_000
MAX_FILE_DIFF_CHARS = 60_000  # one file's diff beyond this is cut off
UNIT_MAX_CHARS = 60_000  # files are packed into review units up to this size


class GitError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} timed out") from e
    if check and r.returncode != 0:
        raise GitError(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout


@contextmanager
def temp_worktree(root: Path, rev: str, prefix: str = "duhview-") -> Iterator[Path]:
    """Check `rev` out into a temporary worktree; the user's checkout and uncommitted work are untouched."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    git(root, "worktree", "add", "--detach", str(tmp), rev)
    try:
        yield tmp
    finally:
        try:
            git(root, "worktree", "remove", "--force", str(tmp))
        except GitError:
            shutil.rmtree(tmp, ignore_errors=True)
            git(root, "worktree", "prune", check=False)


def repo_root(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()


@dataclass
class Target:
    staged: bool = False
    branch: str | None = None
    commit: str | None = None
    base: str | None = None  # plain `base..HEAD` diff, e.g. the commits a push adds

    def label(self) -> str:
        if self.base:
            return f"{self.base[:7]}..HEAD"
        if self.staged:
            return "staged changes"
        if self.branch:
            return f"{self.branch}...HEAD"
        if self.commit:
            return f"commit {self.commit}"
        return "uncommitted changes"


@dataclass
class Change:
    target: str
    files: dict[str, str]  # path -> that file's diff chunk
    truncated: list[str] = field(default_factory=list)  # files whose diff was cut off

    @property
    def diff(self) -> str:
        return "".join(self.files.values())


_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def split_diff(diff: str) -> dict[str, str]:
    starts = list(_FILE_HEADER.finditer(diff))
    return {
        m.group(2): diff[m.start() : starts[i + 1].start() if i + 1 < len(starts) else len(diff)]
        for i, m in enumerate(starts)
    }


def _has_head(root: Path) -> bool:
    return bool(git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())


def _untracked_diff(root: Path) -> str:
    out = []
    for f in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if f:
            # --no-index exits 1 when files differ, which they always do here.
            out.append(git(root, "diff", "--no-index", "--", "/dev/null", f, check=False))
    return "".join(out)


def get_change(root: Path, target: Target, cfg: Config) -> Change:
    if target.base:
        diff = git(root, "diff", target.base, "HEAD")
    elif target.staged:
        diff = git(root, "diff", "--cached")
    elif target.branch:
        diff = git(root, "diff", f"{target.branch}...HEAD")
    elif target.commit:
        diff = git(root, "show", "--format=", target.commit)
    else:
        tracked = git(root, "diff", "HEAD") if _has_head(root) else git(root, "diff", "--cached")
        diff = tracked + _untracked_diff(root)

    files = {p: d for p, d in split_diff(diff).items() if not any(fnmatch(p, g) for g in cfg.review.exclude)}
    truncated = [p for p, d in files.items() if len(d) > MAX_FILE_DIFF_CHARS]
    for p in truncated:
        files[p] = files[p][:MAX_FILE_DIFF_CHARS] + "\n... (diff truncated)\n"
    return Change(target.label(), files, truncated)


def review_units(change: Change) -> list[dict[str, str]]:
    """Pack changed files, in path order so directories stay together, into size-capped units."""
    units: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    size = 0
    for path in sorted(change.files):
        d = change.files[path]
        if cur and size + len(d) > UNIT_MAX_CHARS:
            units.append(cur)
            cur, size = {}, 0
        cur[path] = d
        size += len(d)
    if cur:
        units.append(cur)
    return units


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def hunk_lines(file_diff: str) -> tuple[set[int], dict[int, str]]:
    """(lines the diff adds, new-file line number -> text for every line shown in the diff).
    GitHub only accepts review comments on shown lines."""
    added: set[int] = set()
    shown: dict[int, str] = {}
    new: int | None = None
    for line in file_diff.splitlines():
        if m := _HUNK.match(line):
            new = int(m.group(1))
        elif new is None or line.startswith("\\"):  # before the first hunk, or "\ No newline"
            continue
        elif not line.startswith("-"):
            if line.startswith("+"):
                added.add(new)
            shown[new] = line[1:]
            new += 1
    return added, shown


def added_lines(file_diff: str) -> set[int]:
    """Line numbers in the new file that this diff adds or changes."""
    return hunk_lines(file_diff)[0]


def _repo_file(root: Path, name: str) -> str | None:
    p = (root / name).resolve()  # a symlink must not pull in files from outside the repo
    if p.is_relative_to(root) and p.is_file():
        return p.read_text(errors="replace")[:MAX_CONTEXT_CHARS]
    return None


def worktree_reader(root: Path) -> Reader:
    return lambda name: _repo_file(root, name)


def revision_reader(root: Path, rev: str) -> Reader:
    """Read files as they are at `rev`, e.g. a PR's base commit, regardless of what is checked out."""
    def read(name: str) -> str | None:
        try:
            return git(root, "show", f"{rev}:{name}")[:MAX_CONTEXT_CHARS]
        except GitError:  # not present at that revision
            return None
    return read


def project_context(root: Path, cfg: Config, changed: list[str], spec: Path | None,
                    read_trusted: Reader | None = None) -> str:
    read_trusted = read_trusted or worktree_reader(root)
    parts = []
    for name in CONTEXT_FILES:
        if (text := _repo_file(root, name)) is not None:
            parts.append(f"## {name}\n{text}")
    if (team := read_trusted(RULES_FILE)) is not None:
        parts.append(f"## {RULES_FILE}\n{team}")
    rules = [r.text for r in cfg.rules if not r.paths or any(fnmatch(f, g) for f in changed for g in r.paths)]
    if rules:
        parts.append("## Team rules\n" + "\n".join(f"- {r}" for r in rules))
    if (fp := read_trusted(FP_FILTER_FILE)) is not None:
        parts.append(f"## Do not report (team false-positive filter)\n{fp}")
    if spec:
        parts.append(f"## Requirements / spec ({spec.name})\n{spec.read_text(errors='replace')[:MAX_CONTEXT_CHARS]}")
    return "\n\n".join(parts)
