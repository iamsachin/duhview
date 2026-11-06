"""The git pre-push hook: which commits a push adds, installing the hook, recording bypasses."""

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from duhview.archive import Archive
from duhview.collect import git

HOOK_MARKER = "# installed by duhview"
ZERO = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree
BYPASS_ENV = "DUHVIEW"  # DUHVIEW=skip or DUHVIEW=vouch git push


@dataclass
class PushRange:
    ref: str  # the remote ref being updated
    base: str  # diff base: what the remote already has (or the empty tree)
    head: str  # the commit being pushed


def _is_commit(root: Path, sha: str) -> bool:
    return git(root, "cat-file", "-t", sha, check=False).strip() == "commit"


def push_ranges(root: Path, remote: str, stdin: str) -> list[PushRange]:
    """Parse what git passes a pre-push hook: '<local ref> <local sha> <remote ref> <remote sha>' per line."""
    ranges = []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:  # malformed, or a branch deletion: nothing to review
            continue
        _, local, remote_ref, remote_sha = parts
        if remote_sha != ZERO and _is_commit(root, remote_sha):
            # Normal push: what the remote has. Force push: where the two histories meet.
            base = git(root, "merge-base", remote_sha, local, check=False).strip() or remote_sha
        else:
            # New branch: review only commits no branch on this remote already has.
            new = git(root, "rev-list", "--reverse", local, "--not", f"--remotes={remote}").split()
            if not new:
                continue
            parents = git(root, "rev-list", "--parents", "-n", "1", new[0]).split()[1:]
            base = parents[0] if parents else EMPTY_TREE
        if base != local:
            ranges.append(PushRange(remote_ref, base, local))
    return ranges


def hook_script() -> str:
    # Absolute interpreter path: git GUIs and IDEs often run hooks without the user's PATH.
    return (f"#!/bin/sh\n{HOOK_MARKER}. Skip once (recorded): DUHVIEW=skip git push "
            f"| DUHVIEW=vouch git push\nexec \"{sys.executable}\" -m duhview hook-run \"$@\"\n")


def install_hook(root: Path, force: bool) -> str:
    path = Path(git(root, "rev-parse", "--git-path", "hooks/pre-push").strip())
    path = path if path.is_absolute() else root / path
    if path.exists() and HOOK_MARKER not in path.read_text(errors="replace"):
        if not force:
            raise ValueError(f"{path} already exists and is not duhview's. Add this line to it instead:\n"
                           f"  \"{sys.executable}\" -m duhview hook-run \"$@\" || exit 1\n"
                           "or rerun with --force to replace it (a .bak copy is kept).")
        path.rename(path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hook_script())
    path.chmod(0o755)
    return str(path)


def record_bypass(root: Path, mode: str, ranges: list[PushRange]) -> Path:
    """Bypasses are allowed but never silent: each one is written to the run archive."""
    who = git(root, "config", "user.email", check=False).strip() or git(root, "config", "user.name",
                                                                        check=False).strip()
    archive = Archive(root)
    archive.write("bypass.json", json.dumps({
        "mode": mode, "at": datetime.now(UTC).isoformat(timespec="seconds"), "by": who,
        "pushed": [{"ref": r.ref, "base": r.base, "head": r.head} for r in ranges],
    }, indent=2))
    return archive.dir
