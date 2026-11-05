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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duhview.collect import Change, GitError, git, hunk_lines
from duhview.types import Finding, ReviewResult, Severity

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


def _marker(**meta: Any) -> str:
    return f"<!-- duhview:{json.dumps(meta, separators=(',', ':'))} -->"


_MARKER_RE = re.compile(r"<!-- duhview:(\{.*?\}) -->")


def _markers(body: str) -> list[dict[str, Any]]:
    out = []
    for m in _MARKER_RE.finditer(body or ""):
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return out


_BADGE = {Severity.critical: "🔴 Critical", Severity.major: "🟠 Major", Severity.minor: "🟡 Minor",
          Severity.nitpick: "⚪ Nitpick", Severity.info: "🔵 Info"}


def _heading(f: Finding) -> str:
    tags = [f"**{_BADGE[f.severity]}**", str(f.category).capitalize()]
    if f.source != "agent":
        tags.append(f"found by {f.source.removeprefix('scanner:')}")
    return " · ".join(tags)


def _comment_body(f: Finding, original_first_line: str | None) -> str:
    """`original_first_line` is given only when the whole range is visible, so a suggestion can apply."""
    out = [_heading(f), "", f"**{_safe(f.title)}**", "", _safe(f.explanation)]
    if original_first_line is not None and f.suggestion is not None:
        out += ["", "<sub>Suggested fix (use <b>Commit suggestion</b> to apply):</sub>", "",
                _suggestion(_reindent(f.suggestion, original_first_line))]
    return "\n".join(out + ["", _marker(cat=str(f.category), sev=str(f.severity))])


def _details(title: str, lines: list[str]) -> list[str]:
    return ["", f"<details><summary>{title}</summary>", "", *lines, "", "</details>"]


def _summary(r: ReviewResult, inline: int, rest: list[Finding], progress: str | None) -> str:
    counts = {s: sum(f.severity == s for f in r.findings) for s in Severity}
    if r.incomplete:
        verdict = "⚠️ review incomplete"
    elif not r.findings:
        verdict = "✅ no issues found"
    else:
        verdict = f"{len(r.findings)} issue(s) found"
    out = [MARKER, f"## duhview: {verdict}"]
    if r.findings:
        used = [s for s in Severity if counts[s]]
        out += ["", "| " + " | ".join(_BADGE[s] for s in used) + " |", "|" + "---|" * len(used),
                "| " + " | ".join(str(counts[s]) for s in used) + " |"]
    if progress:
        out += ["", progress]
    where = f"{inline} inline comment(s)" + (f", {len(rest)} below" if rest else "")
    verifier = f" + verifier `{r.verifier_model}`" if r.verifier_model else ""
    out += ["", f"<sub>{where} · {r.units} review unit(s) · `{r.model}`{verifier} · ${r.cost_usd:.4f}</sub>"]
    if r.incomplete:
        out += ["", "**Not fully reviewed:**", *(f"- {_safe(i)}" for i in r.incomplete)]
    if rest:
        out += _details(f"Findings outside the diff ({len(rest)})", [
            f"- {_heading(f)}: `{f.file}:{f.start_line}` **{_safe(f.title)}**  \n  {_safe(f.explanation)} "
            + _marker(file=f.file, line=f.start_line, cat=str(f.category), sev=str(f.severity))
            for f in rest])
    if r.notes:
        out += _details("Notes", [f"- {_safe(n)}" for n in r.notes])
    return "\n".join(out)


def build_review(r: ReviewResult, change: Change, inline: bool = True, findings: list[Finding] | None = None,
                 progress: str | None = None) -> dict[str, Any]:
    """One COMMENT review: findings on lines shown in the diff become inline comments (with a
    one-click suggestion when the whole range is shown); the rest go in the summary."""
    comments: list[dict[str, Any]] = []
    rest: list[Finding] = []
    for f in r.findings if findings is None else findings:
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
    return {"event": "COMMENT", "body": _summary(r, len(comments), rest, progress), "comments": comments}


def post_review(pr: PullRequest, r: ReviewResult, change: Change, tok: str,
                findings: list[Finding] | None = None, progress: str | None = None) -> str:
    """Post the review; if GitHub rejects an inline position, post everything in the summary instead."""
    path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
    try:
        resp = api("POST", path, tok, {"commit_id": pr.head_sha, **build_review(r, change, True, findings, progress)})
    except GitHubError as e:
        if " 422 " not in str(e):
            raise
        resp = api("POST", path, tok,
                   {"commit_id": pr.head_sha, **build_review(r, change, False, findings, progress)})
    return str(resp.get("html_url", ""))


# --- re-runs: post only what is new --------------------------------------------------------

MATCH_LINES = 3  # how far a finding may drift from an earlier comment and still be "the same"


@dataclass
class Thread:
    """An earlier inline comment by duhview, with its replies."""
    id: str  # GraphQL node id, to resolve it
    root_id: int  # REST id of the first comment, to reply to it
    path: str
    line: int | None  # current line, or the original one when the code there changed
    category: str
    severity: Severity  # most severe recorded in the thread
    resolved: bool
    outdated: bool  # GitHub: the commented lines themselves changed
    commit: str = ""  # commit the comment was posted on
    touched: bool = False  # code within MATCH_LINES of the comment changed since `commit`
    fixed_noted: bool = False  # duhview already replied that it looks fixed


@dataclass
class Listed:
    """A finding listed in an earlier summary (it had no line in the diff to comment on)."""
    path: str
    line: int
    category: str


@dataclass
class SyncPlan:
    new: list[Finding]
    worse: list[tuple[Thread, Finding]]
    fixed: list[Thread]
    open: int  # earlier threads still open after this run


def _same(path: str, line: int | None, category: str, f: Finding) -> bool:
    return (path == f.file and category == f.category and line is not None
            and f.start_line - MATCH_LINES <= line <= f.end_line + MATCH_LINES)


def plan_sync(r: ReviewResult, change: Change, threads: list[Thread], listed: list[Listed]) -> SyncPlan:
    """Decide what to post on a re-run. Threads a person resolved are left alone. A thread is only
    called fixed when the finding is gone AND the code under it changed (or left the diff), so a
    model simply not repeating itself never closes a real issue; and never on an incomplete run."""
    used: set[str] = set()
    new: list[Finding] = []
    worse: list[tuple[Thread, Finding]] = []
    for f in r.findings:
        t = next((t for t in threads if t.id not in used and _same(t.path, t.line, t.category, f)), None)
        if t is not None:
            used.add(t.id)
            if not t.resolved and f.severity.rank < t.severity.rank:
                worse.append((t, f))
        elif not any(_same(x.path, x.line, x.category, f) for x in listed):
            new.append(f)
    unresolved = [t for t in threads if not t.resolved]
    fixed = [] if r.incomplete else [t for t in unresolved if t.id not in used
                                     and (t.outdated or t.touched or t.path not in change.files)]
    return SyncPlan(new, worse, fixed, len(unresolved) - len(fixed))


_RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }"
_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes { id isResolved isOutdated path line originalLine
                comments(first: 50) { nodes { databaseId body originalCommit { oid } } } }
      }
    }
  }
}"""


def _graphql(query: str, variables: dict[str, Any], tok: str) -> Any:
    resp = api("POST", "/graphql", tok, {"query": query, "variables": variables})
    if resp.get("errors"):
        raise GitHubError(f"GitHub GraphQL: {resp['errors'][0].get('message')}")
    return resp["data"]


def existing_threads(pr: PullRequest, tok: str) -> list[Thread]:
    # shortcut: reads the first 100 threads; paginate if PRs ever carry more review threads.
    data = _graphql(_THREADS_QUERY, {"owner": pr.owner, "repo": pr.repo, "number": pr.number}, tok)
    threads = []
    for n in data["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        comments = n["comments"]["nodes"]
        if not comments or not (root := _markers(comments[0]["body"])):
            continue  # not started by duhview
        marks = [m for c in comments for m in _markers(c["body"])]
        sevs = [Severity(m["sev"]) for m in marks if m.get("sev") in Severity]
        threads.append(Thread(n["id"], comments[0]["databaseId"], n["path"], n["line"] or n["originalLine"],
                              root[0].get("cat", ""), min(sevs, key=lambda s: s.rank), n["isResolved"],
                              n["isOutdated"], (comments[0].get("originalCommit") or {}).get("oid", ""),
                              fixed_noted=any(m.get("fixed") for m in marks)))
    return threads


def mark_touched(root: Path, head: str, threads: list[Thread]) -> None:
    """A fix often adds lines next to the flagged one (a guard above it) without editing it, which
    GitHub does not count as outdated. Check the diff since the comment's commit around its line."""
    for t in threads:
        if t.resolved or t.outdated or t.line is None or not t.commit:
            continue
        try:
            if git(root, "cat-file", "-t", t.commit, check=False).strip() != "commit":
                # After a rebase or force-push the comment's commit is no longer on the branch;
                # GitHub still serves it by hash.
                git(root, "fetch", "--no-tags", "origin", t.commit, timeout=FETCH_TIMEOUT_S)
            added = hunk_lines(git(root, "diff", t.commit, head, "--", t.path))[0]
        except GitError:
            continue  # commit not available: stay conservative, leave the thread open
        t.touched = any(abs(line - t.line) <= MATCH_LINES for line in added)


def listed_findings(pr: PullRequest, tok: str) -> list[Listed]:
    reviews = api("GET", f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews?per_page=100", tok)
    return [Listed(m["file"], m["line"], m.get("cat", "")) for rv in reviews if MARKER in (rv.get("body") or "")
            for m in _markers(rv["body"]) if "file" in m and "line" in m]


def sync_review(pr: PullRequest, r: ReviewResult, change: Change, tok: str, root: Path,
                resolve: bool = True) -> str:
    """Post new findings, reply on threads that got worse, note (and, if `resolve`, resolve) threads
    that were fixed. `root` is a checkout containing the PR's commits. The default Actions token
    cannot resolve threads, so the Action passes resolve=False and leaves that click to a person."""
    threads = existing_threads(pr, tok)
    mark_touched(root, pr.head_sha, threads)
    plan = plan_sync(r, change, threads, listed_findings(pr, tok))
    done: list[str] = []
    if plan.new:
        progress = (f"{len(plan.new)} new · {plan.open} still open from earlier · {len(plan.fixed)} fixed"
                    if plan.open or plan.fixed else None)
        done.append(f"posted {len(plan.new)} new finding(s): {post_review(pr, r, change, tok, plan.new, progress)}")
    else:
        done.append(f"nothing new to post ({plan.open} earlier finding(s) still open)")

    # Thread updates are best-effort: one failing must not stop the others or fail the review.
    failed: list[str] = []
    replies = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/comments"

    def attempt(what: str, fn: Callable[..., Any], *args: Any) -> bool:
        try:
            fn(*args)
            return True
        except GitHubError as e:
            failed.append(f"{what}: {e}")
            return False

    worse = 0
    for t, f in plan.worse:
        body = (f"⬆️ **Now {_BADGE[f.severity]}** (was {t.severity}): **{_safe(f.title)}**\n\n{_safe(f.explanation)}"
                f"\n\n{_marker(cat=str(f.category), sev=str(f.severity))}")
        worse += attempt("reply", api, "POST", f"{replies}/{t.root_id}/replies", tok, {"body": body})
    resolved = 0
    for t in plan.fixed:
        if not t.fixed_noted:  # never say it twice, e.g. when resolving failed last time
            ask = "" if resolve else " Resolve this thread if you agree."
            note = f"✅ No longer found, and this code changed in `{pr.head_sha[:7]}`.{ask}\n\n{_marker(fixed=True)}"
            attempt("reply", api, "POST", f"{replies}/{t.root_id}/replies", tok, {"body": note})
        if resolve:
            resolved += attempt("resolve thread", _graphql, _RESOLVE, {"id": t.id}, tok)
    if worse:
        done.append(f"{worse} got worse (replied)")
    if plan.fixed:
        done.append(f"{len(plan.fixed)} fixed ({resolved} resolved)" if resolve else f"{len(plan.fixed)} fixed (noted)")
    if failed:
        hint = (" (the token may not be allowed to resolve review threads)"
                if any("not accessible" in f for f in failed) else "")
        done.append(f"warning: {len(failed)} thread update(s) failed{hint}: {failed[0]}")
    return "; ".join(done)
