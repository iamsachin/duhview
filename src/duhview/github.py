"""GitHub pull requests: read PR metadata, check out the PR head, post the review."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duhview.collect import Change, GitError, git, hunk_lines
from duhview.types import Finding, ReviewResult

API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
MARKER = "<!-- duhview -->"  # lets later runs find their own reviews
FETCH_TIMEOUT_S = 180
_PR_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    pass


@dataclass
class PullRequest:
    owner: str
    repo: str
    number: int
    base_ref: str
    base_sha: str
    head_sha: str
    head_repo: str  # "owner/name" the head branch lives in

    @property
    def is_fork(self) -> bool:
        return self.head_repo.lower() != f"{self.owner}/{self.repo}".lower()


def parse_pr_ref(ref: str, root: Path) -> tuple[str, str, int]:
    """A PR URL, or a bare number in the repo named by GITHUB_REPOSITORY or the `origin` remote."""
    if m := _PR_URL.search(ref):
        return m.group(1), m.group(2), int(m.group(3))
    if not ref.lstrip("#").isdigit():
        raise GitHubError(f"not a pull request URL or number: {ref}")
    if full := os.environ.get("GITHUB_REPOSITORY"):
        owner, repo = full.split("/", 1)
        return owner, repo, int(ref.lstrip("#"))
    remote = git(root, "remote", "get-url", "origin", check=False).strip()
    if not (m := _REMOTE.search(remote)):
        raise GitHubError("cannot tell which GitHub repo this is; pass the full PR URL")
    return m.group(1), m.group(2), int(ref.lstrip("#"))


def token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if tok := os.environ.get(var):
            return tok
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    raise GitHubError("no GitHub token: set GITHUB_TOKEN or log in with `gh auth login`")


def api(method: str, path: str, tok: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        f"{API_URL}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {tok}",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "duhview"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise GitHubError(f"GitHub API {method} {path}: {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise GitHubError(f"GitHub API {method} {path}: {e.reason}") from e


def get_pr(owner: str, repo: str, number: int, tok: str) -> PullRequest:
    d = api("GET", f"/repos/{owner}/{repo}/pulls/{number}", tok)
    return PullRequest(owner, repo, number, d["base"]["ref"], d["base"]["sha"], d["head"]["sha"],
                       (d["head"].get("repo") or {}).get("full_name", ""))


@contextmanager
def pr_checkout(root: Path, pr: PullRequest) -> Iterator[Path]:
    """Fetch the PR and check its head out into a temporary worktree; the user's checkout is untouched."""
    # shortcut: assumes the remote is named `origin`; add a --remote option if someone needs another.
    git(root, "fetch", "--no-tags", "origin",
        f"+refs/pull/{pr.number}/head:refs/duhview/pr/{pr.number}/head",
        f"+refs/heads/{pr.base_ref}:refs/duhview/pr/{pr.number}/base", timeout=FETCH_TIMEOUT_S)
    for sha in (pr.base_sha, pr.head_sha):
        git(root, "cat-file", "-e", f"{sha}^{{commit}}")  # raises if the fetch did not bring it
    tmp = Path(tempfile.mkdtemp(prefix=f"duhview-pr{pr.number}-")).resolve()
    git(root, "worktree", "add", "--detach", str(tmp), pr.head_sha)
    try:
        yield tmp
    finally:
        try:
            git(root, "worktree", "remove", "--force", str(tmp))
        except GitError:
            shutil.rmtree(tmp, ignore_errors=True)
            git(root, "worktree", "prune", check=False)


# --- the review --------------------------------------------------------------------------------


def _safe(text: str) -> str:
    # Model output is shaped by the PR's (untrusted) content: never let it @-mention people.
    return text.replace("@", "@\u200b")


def _suggestion(code: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}suggestion\n{code}\n{fence}"


def _reindent(code: str, original_first_line: str) -> str:
    """Models often drop leading indentation; a one-click suggestion must keep the file's.
    Keep the suggestion's relative indentation and anchor it at the original first line's."""
    indent = original_first_line[: len(original_first_line) - len(original_first_line.lstrip())]
    return "\n".join(indent + line if line.strip() else "" for line in textwrap.dedent(code).splitlines())


def _comment_body(f: Finding, original_first_line: str | None) -> str:
    """`original_first_line` is given only when the whole range is visible, so a suggestion can apply."""
    out = f"**[{f.severity}] {_safe(f.title)}** · {f.category}\n\n{_safe(f.explanation)}"
    if original_first_line is not None and f.suggestion is not None:
        out += "\n\n" + _suggestion(_reindent(f.suggestion, original_first_line))
    return out


def _summary(r: ReviewResult, inline: int, rest: list[Finding]) -> str:
    verdict = "review incomplete" if r.incomplete else (f"{len(r.findings)} finding(s)" if r.findings else "no findings")
    stats = (f"{inline} inline · model `{r.model}` · verifier `{r.verifier_model or 'off'}` · "
             f"{r.units} review unit(s) · cost ${r.cost_usd:.4f}")
    out = [MARKER, f"### duhview: {verdict}", "", stats]
    if r.incomplete:
        out += ["", "**Not fully reviewed:**", *(f"- {_safe(i)}" for i in r.incomplete)]
    if rest:
        out += ["", "**Findings outside the diff:**"]
        out += [f"- **[{f.severity}]** `{f.file}:{f.start_line}` {_safe(f.title)}: {_safe(f.explanation)}"
                for f in rest]
    if r.notes:
        out += ["", *(f"_{_safe(n)}_" for n in r.notes)]
    return "\n".join(out)


def build_review(r: ReviewResult, change: Change, inline: bool = True) -> dict[str, Any]:
    """One COMMENT review: findings on lines shown in the diff become inline comments (with a
    one-click suggestion when the whole range is shown); the rest go in the summary."""
    comments: list[dict[str, Any]] = []
    rest: list[Finding] = []
    for f in r.findings:
        shown = hunk_lines(change.files.get(f.file, ""))[1]
        span = range(f.start_line, max(f.end_line, f.start_line) + 1)
        whole = all(line in shown for line in span)
        if not inline or not (whole or f.end_line in shown or f.start_line in shown):
            rest.append(f)
            continue
        c: dict[str, Any] = {"path": f.file, "side": "RIGHT",
                             "body": _comment_body(f, shown[span[0]] if whole else None)}
        if whole and len(span) > 1:
            c |= {"start_line": span[0], "start_side": "RIGHT", "line": span[-1]}
        else:
            c["line"] = span[-1] if whole or f.end_line in shown else f.start_line
        comments.append(c)
    return {"event": "COMMENT", "body": _summary(r, len(comments), rest), "comments": comments}


def post_review(pr: PullRequest, r: ReviewResult, change: Change, tok: str) -> str:
    """Post the review; if GitHub rejects an inline position, post everything in the summary instead."""
    path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
    try:
        resp = api("POST", path, tok, {"commit_id": pr.head_sha, **build_review(r, change)})
    except GitHubError as e:
        if " 422 " not in str(e):
            raise
        resp = api("POST", path, tok, {"commit_id": pr.head_sha, **build_review(r, change, inline=False)})
    return str(resp.get("html_url", ""))
