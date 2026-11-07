"""Answer @duhview comments on pull requests, and replies on duhview's own review threads."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from duhview import model as llm
from duhview.agent import ask_json
from duhview.archive import Archive
from duhview.collect import Change, Reader, project_context
from duhview.config import Config
from duhview.github import MARKER, PullRequest, _marker, _markers, _safe, api, api_pages
from duhview.review import _fence, _prompt
from duhview.tools import RepoTools

# Comments can trigger paid model calls, and comment workflows get secrets even on fork PRs.
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
OWN = "<!-- duhview"  # every comment duhview posts carries a hidden marker starting with this
MAX_TOOL_CALLS = 15
MAX_DIFF_CHARS = 60_000
_COMMAND = re.compile(r"@duhview\s+(review|explain)\b[\s:]*(.*)", re.IGNORECASE | re.DOTALL)


@dataclass
class Request:
    kind: Literal["review", "explain", "reply"]
    pr_number: int
    text: str  # review focus, question, or the reply itself
    comment_id: int
    thread_root: int | None = None  # for comments in a review thread


class Reply(BaseModel):
    verdict: Literal["agree", "disagree", "answer"]
    reply: str


class Answer(BaseModel):
    reply: str


def parse_event(name: str, payload: dict[str, Any]) -> Request | None:
    """The request in a GitHub comment event, or None when duhview should stay quiet."""
    c = payload.get("comment") or {}
    if payload.get("action") != "created" or (c.get("user") or {}).get("type") == "Bot":
        return None
    if c.get("author_association") not in TRUSTED:
        return None
    body = c.get("body") or ""
    if OWN in body:
        # duhview's own comment. With a personal token they are posted as the repo owner, so the
        # author checks above don't catch them; without this, it would answer itself forever.
        return None
    m = _COMMAND.search(body)
    if name == "issue_comment":
        issue = payload.get("issue") or {}
        if not issue.get("pull_request") or not m:
            return None
        return Request(m.group(1).lower(), issue["number"], m.group(2).strip(), c["id"])  # type: ignore[arg-type]
    if name == "pull_request_review_comment":
        number = payload["pull_request"]["number"]
        root = c.get("in_reply_to_id") or c["id"]
        if m:
            return Request(m.group(1).lower(), number, m.group(2).strip(), c["id"], root)  # type: ignore[arg-type]
        if c.get("in_reply_to_id"):
            return Request("reply", number, body, c["id"], root)
    return None


def _reply_in_thread(pr: PullRequest, root_id: int, body: str, tok: str) -> None:
    api("POST", f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/comments/{root_id}/replies", tok, {"body": body})


async def answer(req: Request, pr: PullRequest, change: Change, root: Path, cfg: Config, model: str, tok: str,
                 read_trusted: Reader | None, archive_root: Path) -> str:
    """Handle an `explain` or a thread reply. `root` is a checkout of the PR head."""
    budget = llm.Budget(cfg.budget.max_usd_per_review)
    tools = RepoTools(root)
    log: list[Any] = []
    context = project_context(root, cfg, list(change.files), None, read_trusted)
    ctx = _fence("project_context", context) + "\n\n" if context else ""
    try:
        if req.kind == "reply":
            return await _converse(req, pr, change, ctx, model, tools, budget, log, tok)
        return await _explain(req, pr, change, ctx, model, tools, budget, log, tok)
    finally:
        Archive(archive_root).write("conversations.json", log)


async def _converse(req: Request, pr: PullRequest, change: Change, ctx: str, model: str, tools: RepoTools,
                    budget: llm.Budget, log: list[Any], tok: str) -> str:
    base = f"/repos/{pr.owner}/{pr.repo}/pulls"
    root = api("GET", f"{base}/comments/{req.thread_root}", tok)
    if not _markers(root.get("body", "")):
        return "not a duhview thread; ignored"
    thread = [c for c in api_pages(f"{base}/{pr.number}/comments", tok) if c.get("in_reply_to_id") == req.thread_root]
    convo = "\n\n".join(f"**{c['user']['login']}:** {c['body']}" for c in thread)
    user = (ctx + _fence("finding", f"{root['path']}:{root.get('line') or root.get('original_line')}\n{root['body']}")
            + "\n\n" + _fence("thread", convo) + "\n\n"
            + _fence("untrusted_diff", change.files.get(root["path"], "(file not changed in this PR)")))
    out = await ask_json(model, _prompt("responder"), user, Reply, tools, budget, MAX_TOOL_CALLS, log)
    body = _safe(out.reply)
    if out.verdict == "agree":
        # Remembered: later runs on this PR won't raise it again, and it no longer fails the check.
        body += "\n\n_Marked as not an issue for this pull request._\n\n" + _marker(dismissed=True)
    _reply_in_thread(pr, int(req.thread_root or 0), body + "\n\n" + MARKER, tok)
    return f"replied in thread ({out.verdict})"


MAX_DISCUSSION_CHARS = 20_000


def _discussion(pr: PullRequest, tok: str) -> str:
    """Conversations on duhview's findings, so an answer doesn't contradict what the team settled.
    Seen live: `explain` called something a bug right after a maintainer explained it was intended."""
    comments = api_pages(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/comments", tok)
    roots = {c["id"]: c for c in comments if not c.get("in_reply_to_id") and _markers(c.get("body", ""))}
    lines = []
    for rid, root in roots.items():
        title = next((m.get("title") for m in _markers(root["body"]) if m.get("title")), "finding")
        replies = [c for c in comments if c.get("in_reply_to_id") == rid]
        if replies:
            lines.append(f"### {root['path']}:{root.get('line') or root.get('original_line')}: {title}")
            lines += [f"**{c['user']['login']}:** {c['body'].split('<!--')[0].strip()}" for c in replies]
    text = "\n\n".join(lines)[:MAX_DISCUSSION_CHARS]
    return _fence("discussion", text) if text else ""


async def _explain(req: Request, pr: PullRequest, change: Change, ctx: str, model: str, tools: RepoTools,
                   budget: llm.Budget, log: list[Any], tok: str) -> str:
    location = ""
    if req.thread_root is not None:
        c = api("GET", f"/repos/{pr.owner}/{pr.repo}/pulls/comments/{req.comment_id}", tok)
        location = _fence("location", f"{c['path']}:{c.get('line') or c.get('original_line')}\n{c.get('diff_hunk', '')}")
    discussion = _discussion(pr, tok)
    diff = change.diff
    if len(diff) > MAX_DIFF_CHARS:  # big PR: list the files and let the agent read what it needs
        diff = "Changed files (read them with the tools):\n" + "\n".join(change.files)
    user = (ctx + _fence("question", req.text or "Explain this change.") + "\n\n"
            + (location + "\n\n" if location else "") + (discussion + "\n\n" if discussion else "")
            + _fence("untrusted_diff", diff))
    out = await ask_json(model, _prompt("explainer"), user, Answer, tools, budget, MAX_TOOL_CALLS, log)
    if req.thread_root is not None:
        _reply_in_thread(pr, req.thread_root, _safe(out.reply) + "\n\n" + MARKER, tok)
    else:
        quote = "\n".join(f"> {line}" for line in _safe(req.text or "explain").splitlines())
        api("POST", f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments", tok,
            {"body": f"{quote}\n\n{_safe(out.reply)}\n\n{MARKER}"})
    return "answered"
