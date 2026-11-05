"""Read-only tools the review agent can call.

The agent can only see files git would commit: tracked files plus untracked files that are
not ignored. That keeps it inside the repo and away from .env files and build output.
"""

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from duhview.collect import GitError, git

TOOL_TIMEOUT_S = 10
MAX_READ_BYTES = 200_000
MAX_READ_LINES = 1500
MAX_GREP_FILE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 30_000


class ToolError(Exception):
    pass


def _fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_S = {"type": "string"}
_I = {"type": "integer"}

SCHEMAS = [
    _fn("read_file", "Read a repository file with line numbers.",
        {"path": _S, "start_line": _I, "end_line": _I}, ["path"]),
    _fn("grep", "Search repository files. Literal text unless regex=true. Returns path:line: text.",
        {"pattern": _S, "path": {**_S, "description": "directory to search, default whole repo"},
         "regex": {"type": "boolean"}}, ["pattern"]),
    _fn("list_dir", "List a repository directory.", {"path": _S}, []),
    _fn("git_log", "Recent commits, optionally for one path.", {"path": _S, "n": _I}, []),
    _fn("git_blame", "Blame a line range of a file.",
        {"path": _S, "start_line": _I, "end_line": _I}, ["path", "start_line", "end_line"]),
]


class RepoTools:
    schemas = SCHEMAS

    def __init__(self, root: Path):
        self.root = root.resolve()
        listing = git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        self.files = {f for f in listing.split("\0") if f}
        self._impl: dict[str, Callable[..., str]] = {
            "read_file": self.read_file, "grep": self.grep, "list_dir": self.list_dir,
            "git_log": self.git_log, "git_blame": self.git_blame,
        }

    # --- path jail ---------------------------------------------------------------------------
    def _rel(self, path: str) -> str:
        p = (self.root / path).resolve()  # resolves ".." and symlinks
        if not p.is_relative_to(self.root):
            raise ToolError("path is outside the repository")
        rel = p.relative_to(self.root).as_posix()
        return "" if rel == "." else rel

    def _file(self, path: str) -> tuple[str, Path]:
        rel = self._rel(path)
        if rel not in self.files:
            raise ToolError(f"{path}: not a file in the repository (missing or ignored)")
        return rel, self.root / rel

    def _under(self, rel_dir: str) -> list[str]:
        prefix = f"{rel_dir}/" if rel_dir else ""
        return sorted(f for f in self.files if f.startswith(prefix))

    # --- tools -------------------------------------------------------------------------------
    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        _, p = self._file(path)
        data = p.read_bytes()[:MAX_READ_BYTES]
        if b"\0" in data[:8000]:
            raise ToolError("binary file")
        lines = data.decode(errors="replace").splitlines()
        start = max(start_line, 1)
        end = min(end_line or len(lines), len(lines), start + MAX_READ_LINES - 1)
        out = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
        if end < len(lines):
            out += f"\n... ({len(lines)} lines total; request more with start_line)"
        return out or "(empty)"

    def grep(self, pattern: str, path: str = ".", regex: bool = False) -> str:
        # shortcut: a pathological regex can stall this worker thread past the timeout;
        # move to a subprocess if that ever shows up.
        rx = re.compile(pattern if regex else re.escape(pattern))
        hits: list[str] = []
        for f in self._under(self._rel(path)):
            p = self.root / f
            if not p.is_file() or p.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            data = p.read_bytes()
            if b"\0" in data[:8000]:
                continue
            for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line[:300]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n... (truncated at 200 matches)"
        return "\n".join(hits) or "no matches"

    def list_dir(self, path: str = ".") -> str:
        rel = self._rel(path)
        prefix = f"{rel}/" if rel else ""
        entries = {f[len(prefix):].split("/")[0] + ("/" if "/" in f[len(prefix):] else "")
                   for f in self._under(rel)}
        if not entries:
            raise ToolError(f"{path}: no such directory in the repository")
        return "\n".join(sorted(entries))

    def git_log(self, path: str | None = None, n: int = 10) -> str:
        args = ["log", "--oneline", "-n", str(min(max(n, 1), 50))]
        if path:
            args += ["--", self._rel(path) or "."]
        return git(self.root, *args, check=False) or "no commits"

    def git_blame(self, path: str, start_line: int, end_line: int) -> str:
        rel, _ = self._file(path)
        return git(self.root, "blame", "-L", f"{start_line},{end_line}", "--", rel)

    # --- dispatch ----------------------------------------------------------------------------
    async def call(self, name: str, arguments: str) -> str:
        fn = self._impl.get(name)
        if fn is None:
            return f"error: unknown tool {name}"
        try:
            args = json.loads(arguments or "{}")
            out = await asyncio.wait_for(asyncio.to_thread(fn, **args), TOOL_TIMEOUT_S)
        except TimeoutError:
            return f"error: {name} timed out"
        except (ToolError, GitError, TypeError, ValueError, re.error, OSError) as e:
            return f"error: {e}"
        return out[:MAX_OUTPUT_CHARS] + ("\n... (output truncated)" if len(out) > MAX_OUTPUT_CHARS else "")
