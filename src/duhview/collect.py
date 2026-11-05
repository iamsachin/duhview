"""Collect what to review: the diff, changed files and project context."""

import re
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from duhview.config import Config

CONTEXT_FILES = ["AGENTS.md", "CLAUDE.md", ".duhview/rules.md"]
FP_FILTER_FILE = ".duhview/fp-filter.md"
MAX_CONTEXT_CHARS = 20_000
MAX_DIFF_CHARS = 400_000


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


def repo_root(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()


@dataclass
class Target:
    staged: bool = False
    branch: str | None = None
    commit: str | None = None

    def label(self) -> str:
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
    truncated: bool = False

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
    if target.staged:
        diff = git(root, "diff", "--cached")
    elif target.branch:
        diff = git(root, "diff", f"{target.branch}...HEAD")
    elif target.commit:
        diff = git(root, "show", "--format=", target.commit)
    else:
        tracked = git(root, "diff", "HEAD") if _has_head(root) else git(root, "diff", "--cached")
        diff = tracked + _untracked_diff(root)

    files = {p: d for p, d in split_diff(diff).items() if not any(fnmatch(p, g) for g in cfg.review.exclude)}
    change = Change(target.label(), files)
    if len(change.diff) > MAX_DIFF_CHARS:
        # shortcut: drops whole files past the cap; review-unit splitting will replace this.
        kept, size = {}, 0
        for p, d in files.items():
            if size + len(d) > MAX_DIFF_CHARS:
                break
            kept[p], size = d, size + len(d)
        change = Change(target.label(), kept, truncated=True)
    return change


def _repo_file(root: Path, name: str) -> str | None:
    p = (root / name).resolve()  # a symlink must not pull in files from outside the repo
    if p.is_relative_to(root) and p.is_file():
        return p.read_text(errors="replace")[:MAX_CONTEXT_CHARS]
    return None


def project_context(root: Path, cfg: Config, changed: list[str], spec: Path | None) -> str:
    parts = []
    for name in CONTEXT_FILES:
        if (text := _repo_file(root, name)) is not None:
            parts.append(f"## {name}\n{text}")
    rules = [r.text for r in cfg.rules if not r.paths or any(fnmatch(f, g) for f in changed for g in r.paths)]
    if rules:
        parts.append("## Team rules\n" + "\n".join(f"- {r}" for r in rules))
    if (fp := _repo_file(root, FP_FILTER_FILE)) is not None:
        parts.append(f"## Do not report (team false-positive filter)\n{fp}")
    if spec:
        parts.append(f"## Requirements / spec ({spec.name})\n{spec.read_text(errors='replace')[:MAX_CONTEXT_CHARS]}")
    return "\n\n".join(parts)
