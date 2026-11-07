"""GitHub pull requests: read PR metadata, check out the PR head, post the review."""

import json
import os
import re
import shutil
import subprocess
import textwrap
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duhview.collect import Change, GitError, git, hunk_lines, temp_worktree
from duhview.types import Finding, PRSummary, ReviewResult, Severity

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
    title: str = ""
    body: str = ""  # written by the PR author: untrusted

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
    repos = [r for _, r in _github_remotes(root)]
    if len(set(repos)) != 1:  # none, or several different GitHub repos (e.g. a fork and upstream)
        raise GitHubError("cannot tell which GitHub repo this is; pass the full PR URL")
    owner, repo = repos[0]
    return owner, repo, int(ref.lstrip("#"))


def _github_remotes(root: Path) -> list[tuple[str, tuple[str, str]]]:
    """(remote name, (owner, repo)) for every remote that points at GitHub; `origin` first."""
    names = git(root, "remote", check=False).split()
    out = []
    for name in sorted(names, key=lambda n: n != "origin"):
        if m := _REMOTE.search(git(root, "remote", "get-url", name, check=False).strip()):
            out.append((name, (m.group(1), m.group(2))))
    return out


def find_remote(root: Path, owner: str, repo: str) -> str:
    """The remote for owner/repo, whatever it is called (`origin`, `upstream`, ...)."""
    for name, (o, r) in _github_remotes(root):
        if (o.lower(), r.lower()) == (owner.lower(), repo.lower()):
            return name
    if "origin" in git(root, "remote", check=False).split():
        return "origin"  # e.g. GitHub Enterprise or a mirror URL we can't match by name
    raise GitHubError(f"no git remote points at github.com/{owner}/{repo}; add one with `git remote add`")


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


MAX_PAGES = 50  # 5,000 items; a guard against looping forever on a misbehaving API


def api_pages(path: str, tok: str) -> list[Any]:
    """GET every page of a list endpoint."""
    items: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        batch = api("GET", f"{path}?per_page=100&page={page}", tok)
        items += batch
        if len(batch) < 100:
            break
    return items


def get_pr(owner: str, repo: str, number: int, tok: str) -> PullRequest:
    d = api("GET", f"/repos/{owner}/{repo}/pulls/{number}", tok)
    return PullRequest(owner, repo, number, d["base"]["ref"], d["base"]["sha"], d["head"]["sha"],
                       (d["head"].get("repo") or {}).get("full_name", ""), d.get("title") or "", d.get("body") or "")


@contextmanager
def pr_checkout(root: Path, pr: PullRequest) -> Iterator[Path]:
    """Fetch the PR and check its head out into a temporary worktree; the user's checkout is untouched."""
    git(root, "fetch", "--no-tags", find_remote(root, pr.owner, pr.repo),
        f"+refs/pull/{pr.number}/head:refs/duhview/pr/{pr.number}/head",
        f"+refs/heads/{pr.base_ref}:refs/duhview/pr/{pr.number}/base", timeout=FETCH_TIMEOUT_S)
    for sha in (pr.base_sha, pr.head_sha):
        git(root, "cat-file", "-e", f"{sha}^{{commit}}")  # raises if the fetch did not bring it
    with temp_worktree(root, pr.head_sha, prefix=f"duhview-pr{pr.number}-") as tmp:
        yield tmp


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
    # ">" escaped so model text can never close the HTML comment early.
    data = json.dumps(meta, separators=(',', ':')).replace(">", "\\u003e")
    return f"<!-- duhview:{data} -->"


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
    elif len(f.models) > 1:
        tags.append(f"found by {len(f.models)} models")
    return " · ".join(tags)


def _comment_body(f: Finding, original_first_line: str | None) -> str:
    """`original_first_line` is given only when the whole range is visible, so a suggestion can apply."""
    out = [_heading(f), "", f"**{_safe(f.title)}**", "", _safe(f.explanation)]
    if original_first_line is not None and f.suggestion is not None:
        out += ["", "<sub>Suggested fix (use <b>Commit suggestion</b> to apply):</sub>", "",
                _suggestion(_reindent(f.suggestion, original_first_line))]
    return "\n".join(out + ["", _marker(cat=str(f.category), sev=str(f.severity), title=_safe(f.title))])


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
    title: str = ""  # the finding's title when it was posted
    dismissed: bool = False  # duhview agreed in the thread that the finding was wrong

    @property
    def closed(self) -> bool:
        """A person resolved it, or duhview was convinced it was wrong."""
        return self.resolved or self.dismissed


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
    closed: list[Finding] = field(default_factory=list)  # current findings on resolved/dismissed threads


def _same(path: str, line: int | None, category: str, f: Finding) -> bool:
    return (path == f.file and category == f.category and line is not None
            and f.start_line - MATCH_LINES <= line <= f.end_line + MATCH_LINES)


TITLE_OVERLAP = 0.3
_WORD = re.compile(r"[a-z0-9_]{3,}")


def _similar_titles(a: str, b: str) -> bool:
    """Do two finding titles describe the same problem? Word overlap; models reword titles between
    runs but keep the key terms."""
    wa, wb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= TITLE_OVERLAP


def _same_issue(t: "Thread", f: Finding) -> bool:
    """Same place and category; and if the code there changed, also a similar title. Seen live: a
    different problem near a fixed line was taken for the old one, hiding both. The reviewer is
    asked to re-report open issues under their exact title, so that matches wherever the lines moved."""
    if t.title and t.path == f.file and t.title.strip().lower() == _safe(f.title).strip().lower():
        return True
    if not _same(t.path, t.line, t.category, f):
        return False
    return not (t.outdated or t.touched) or not t.title or _similar_titles(t.title, f.title)


def plan_sync(r: ReviewResult, change: Change, threads: list[Thread], listed: list[Listed]) -> SyncPlan:
    """Decide what to post on a re-run. Threads a person resolved are left alone. A thread is only
    called fixed when the finding is gone AND the code under it changed (or left the diff), so a
    model simply not repeating itself never closes a real issue; and never on an incomplete run."""
    used: set[str] = set()
    new: list[Finding] = []
    worse: list[tuple[Thread, Finding]] = []
    closed: list[Finding] = []
    for f in r.findings:
        t = next((t for t in threads if t.id not in used and _same_issue(t, f)), None)
        if t is not None:
            used.add(t.id)
            if t.closed:
                closed.append(f)
            elif f.severity.rank < t.severity.rank:
                worse.append((t, f))
        elif not any(_same(x.path, x.line, x.category, f) for x in listed):
            new.append(f)
    unresolved = [t for t in threads if not t.closed]
    fixed = [] if r.incomplete else [t for t in unresolved if t.id not in used
                                     and (t.outdated or t.touched or t.path not in change.files)]
    return SyncPlan(new, worse, fixed, len(unresolved) - len(fixed), closed)


_RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }"
_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
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


def _all_thread_nodes(pr: PullRequest, tok: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    after = None
    for _ in range(MAX_PAGES):
        data = _graphql(_THREADS_QUERY, {"owner": pr.owner, "repo": pr.repo, "number": pr.number,
                                         "after": after}, tok)
        page = data["repository"]["pullRequest"]["reviewThreads"]
        nodes += page["nodes"]
        if not page.get("pageInfo", {}).get("hasNextPage"):
            break
        after = page["pageInfo"]["endCursor"]
    return nodes


def existing_threads(pr: PullRequest, tok: str) -> list[Thread]:
    threads = []
    for n in _all_thread_nodes(pr, tok):
        comments = n["comments"]["nodes"]
        if not comments or not (root := _markers(comments[0]["body"])):
            continue  # not started by duhview
        marks = [m for c in comments for m in _markers(c["body"])]
        sevs = [Severity(m["sev"]) for m in marks if m.get("sev") in Severity]
        threads.append(Thread(n["id"], comments[0]["databaseId"], n["path"], n["line"] or n["originalLine"],
                              root[0].get("cat", ""), min(sevs, key=lambda s: s.rank), n["isResolved"],
                              n["isOutdated"], (comments[0].get("originalCommit") or {}).get("oid", ""),
                              fixed_noted=any(m.get("fixed") for m in marks), title=str(root[0].get("title", "")),
                              dismissed=any(m.get("dismissed") for m in marks)))
    return threads


def known_issues(pr: PullRequest, tok: str) -> str:
    """duhview's open findings on this PR, for the reviewer to avoid restating them in new words."""
    lines = [f"- {t.path}:{t.line} [{t.severity}] {t.title}" for t in existing_threads(pr, tok)
             if not t.closed and not t.fixed_noted and t.title]
    return "\n".join(lines)


def mark_touched(root: Path, head: str, threads: list[Thread], remote: str = "origin") -> None:
    """A fix often adds lines next to the flagged one (a guard above it) without editing it, which
    GitHub does not count as outdated. Check the diff since the comment's commit around its line."""
    for t in threads:
        if t.closed or t.outdated or t.line is None or not t.commit:
            continue
        try:
            if git(root, "cat-file", "-t", t.commit, check=False).strip() != "commit":
                # After a rebase or force-push the comment's commit is no longer on the branch;
                # GitHub still serves it by hash.
                git(root, "fetch", "--no-tags", remote, t.commit, timeout=FETCH_TIMEOUT_S)
            added = hunk_lines(git(root, "diff", t.commit, head, "--", t.path))[0]
        except GitError:
            continue  # commit not available: stay conservative, leave the thread open
        t.touched = any(abs(line - t.line) <= MATCH_LINES for line in added)


def listed_findings(pr: PullRequest, tok: str) -> list[Listed]:
    reviews = api_pages(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews", tok)
    return [Listed(m["file"], m["line"], m.get("cat", "")) for rv in reviews if MARKER in (rv.get("body") or "")
            for m in _markers(rv["body"]) if "file" in m and "line" in m]


# --- the summary comment: one per PR, edited in place on every run ---------------------------

SUMMARY_MARKER = "<!-- duhview:summary -->"
_STATE_RE = re.compile(r"<!-- duhview-state:(\{.*?\}) -->", re.DOTALL)


def _state(body: str) -> dict[str, Any]:
    if m := _STATE_RE.search(body or ""):
        try:
            return dict(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    return {}


def summary_body(pr: PullRequest, r: ReviewResult, summary: PRSummary | None, fixed: int,
                 review_url: str, live: list[Finding] | None = None) -> str:
    """`live`: findings still open (not on resolved or dismissed threads); default all."""
    live = r.findings if live is None else live
    out = [SUMMARY_MARKER, "## 📝 duhview summary", ""]
    if summary:
        out += [_safe(summary.overview), "", "**What changed**", *(f"- {_safe(c)}" for c in summary.changes),
                "", f"**Review focus:** {_safe(summary.focus)}"]
    else:
        out.append("_No summary yet._")
    counts = [f"{_BADGE[s]} {n}" for s in Severity if (n := sum(f.severity == s for f in live))]
    status = " · ".join(counts) + " open" if counts else "✅ no open issues"
    if fixed:
        status += f" · {fixed} fixed"
    if review_url:
        status += f" · [latest review]({review_url})"
    out += ["", "---", f"**Status at `{pr.head_sha[:7]}`:** {status}"]
    if r.incomplete:
        out.append("⚠️ The last review was incomplete; see the run log.")
    out.append(f"<sub>Updated on every push · `{r.model}`</sub>")
    # Kept so a later run can reuse the summary if generating it fails. ">" is escaped so the
    # JSON can never close the HTML comment early.
    state = json.dumps({"summary": summary.model_dump() if summary else None, "review": review_url})
    return "\n".join(out) + f"\n<!-- duhview-state:{state.replace('>', '\\u003e')} -->"


def upsert_summary(pr: PullRequest, r: ReviewResult, fixed: int, review_url: str, tok: str,
                   live: list[Finding] | None = None) -> str:
    """Create the summary comment, or edit the existing one in place."""
    comments = api_pages(f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments", tok)
    existing = next((c for c in comments if SUMMARY_MARKER in (c.get("body") or "")), None)
    old = _state(existing["body"]) if existing else {}
    summary = r.summary or (PRSummary.model_validate(old["summary"]) if old.get("summary") else None)
    body = summary_body(pr, r, summary, fixed, review_url or old.get("review", ""), live)
    if existing:
        api("PATCH", f"/repos/{pr.owner}/{pr.repo}/issues/comments/{existing['id']}", tok, {"body": body})
        return "summary updated"
    api("POST", f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments", tok, {"body": body})
    return "summary posted"


@dataclass
class SyncResult:
    message: str
    live: list[Finding]  # findings not on a resolved or dismissed thread: what the check should count


def sync_review(pr: PullRequest, r: ReviewResult, change: Change, tok: str, root: Path,
                resolve: bool = True) -> SyncResult:
    """Post new findings, reply on threads that got worse, note (and, if `resolve`, resolve) threads
    that were fixed. `root` is a checkout containing the PR's commits. The default Actions token
    cannot resolve threads, so the Action passes resolve=False and leaves that click to a person."""
    threads = existing_threads(pr, tok)
    try:
        remote = find_remote(root, pr.owner, pr.repo)
    except GitHubError:
        remote = "origin"  # only used to fetch commits a rebase removed: best effort
    mark_touched(root, pr.head_sha, threads, remote)
    plan = plan_sync(r, change, threads, listed_findings(pr, tok))
    live = [f for f in r.findings if f not in plan.closed]
    done: list[str] = []
    review_url = ""
    if plan.new:
        progress = (f"{len(plan.new)} new · {plan.open} still open from earlier · {len(plan.fixed)} fixed"
                    if plan.open or plan.fixed else None)
        review_url = post_review(pr, r, change, tok, plan.new, progress)
        done.append(f"posted {len(plan.new)} new finding(s): {review_url}")
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
    fixed_so_far = sum(t.fixed_noted for t in threads) + sum(not t.fixed_noted for t in plan.fixed)
    try:
        done.append(upsert_summary(pr, r, fixed_so_far, review_url, tok, live))
    except GitHubError as e:
        failed.append(f"summary: {e}")
    if failed:
        hint = (" (the token may not be allowed to resolve review threads)"
                if any("not accessible" in f for f in failed) else "")
        done.append(f"warning: {len(failed)} thread update(s) failed{hint}: {failed[0]}")
    return SyncResult("; ".join(done), live)
