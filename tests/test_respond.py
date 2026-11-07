import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from duhview import github, model, respond
from duhview.collect import Change
from duhview.config import Config
from duhview.types import Severity

from .test_core import _resp
from .test_github import CHANGE, DIFF, _f, _result, _t

PR = github.PullRequest("o", "r", 7, "main", "b" * 40, "h" * 40, "o/r")


def _event(body: str, assoc: str = "COLLABORATOR", reply_to: int | None = None, bot: bool = False,
           review_comment: bool = False) -> dict[str, Any]:
    c = {"id": 11, "body": body, "author_association": assoc, "user": {"type": "Bot" if bot else "User",
                                                                        "login": "sam"}}
    if reply_to:
        c["in_reply_to_id"] = reply_to
    if review_comment:
        return {"action": "created", "comment": c, "pull_request": {"number": 7}}
    return {"action": "created", "comment": c, "issue": {"number": 7, "pull_request": {"url": "x"}}}


# --- who may trigger what ----------------------------------------------------------------------


def test_parse_commands_from_trusted_people_only() -> None:
    r = respond.parse_event("issue_comment", _event("@duhview review focus on auth"))
    assert r is not None and (r.kind, r.text, r.pr_number) == ("review", "focus on auth", 7)
    r = respond.parse_event("issue_comment", _event("hey @duhview explain why is cache global?"))
    assert r is not None and (r.kind, r.text) == ("explain", "why is cache global?")
    assert respond.parse_event("issue_comment", _event("@duhview review", assoc="CONTRIBUTOR")) is None
    assert respond.parse_event("issue_comment", _event("@duhview review", bot=True)) is None
    assert respond.parse_event("issue_comment", _event("thanks!")) is None
    plain_issue = _event("@duhview review") | {"issue": {"number": 7}}
    assert respond.parse_event("issue_comment", plain_issue) is None


def test_replies_in_review_threads_are_conversations() -> None:
    r = respond.parse_event("pull_request_review_comment", _event("this is intended", reply_to=5, review_comment=True))
    assert r is not None and (r.kind, r.thread_root, r.text) == ("reply", 5, "this is intended")
    r = respond.parse_event("pull_request_review_comment", _event("@duhview explain", reply_to=5, review_comment=True))
    assert r is not None and (r.kind, r.thread_root) == ("explain", 5)
    # A new top-level code comment without a command is not for duhview.
    assert respond.parse_event("pull_request_review_comment", _event("nit", review_comment=True)) is None


# --- the Action's gate (bash + jq), run for real -------------------------------------------------


def _gate(tmp_path: Path, event_name: str, payload: dict[str, Any]) -> dict[str, str]:
    script = next(s["run"] for s in yaml.safe_load(Path("action.yml").read_text())["runs"]["steps"]
                  if s.get("id") == "gate")
    (tmp_path / "event.json").write_text(json.dumps(payload))
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
           "GITHUB_EVENT_NAME": event_name, "GITHUB_REPOSITORY": "o/r", "GITHUB_OUTPUT": str(out)}
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
    return dict(line.split("=", 1) for line in out.read_text().split())


@pytest.mark.skipif(not shutil.which("jq"), reason="jq not installed")
def test_action_gate(tmp_path: Path) -> None:
    same = {"pull_request": {"head": {"repo": {"full_name": "o/r"}}}}
    fork = {"pull_request": {"head": {"repo": {"full_name": "someone/r"}}}}
    assert _gate(tmp_path, "pull_request", same) == {"mode": "review", "scanners": "true"}
    assert _gate(tmp_path, "pull_request", fork)["mode"] == "skip"
    assert _gate(tmp_path, "issue_comment", _event("@duhview review")) == {"mode": "respond", "scanners": "true"}
    assert _gate(tmp_path, "issue_comment", _event("@duhview explain x")) == {"mode": "respond", "scanners": "false"}
    assert _gate(tmp_path, "issue_comment", _event("@duhview review", assoc="NONE"))["mode"] == "skip"
    assert _gate(tmp_path, "issue_comment", _event("@duhview review", bot=True))["mode"] == "skip"
    assert _gate(tmp_path, "issue_comment", _event("lgtm"))["mode"] == "skip"
    reply = _event("intended", reply_to=5, review_comment=True)
    assert _gate(tmp_path, "pull_request_review_comment", reply)["mode"] == "respond"


# --- conversations -----------------------------------------------------------------------------

FINDING_BODY = github.build_review(_result(_f(11, 11)), CHANGE)["comments"][0]["body"]


def _fake_github(monkeypatch: pytest.MonkeyPatch, root_body: str) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []

    def fake_api(method: str, path: str, tok: str, body: dict[str, Any] | None = None) -> Any:
        calls.append((method, path, body))
        if path.endswith("/comments/5"):
            return {"id": 5, "path": "a.py", "line": 11, "body": root_body}
        return {}

    monkeypatch.setattr(respond, "api", fake_api)
    monkeypatch.setattr(respond, "api_pages", lambda path, tok: [
        {"id": 5, "path": "a.py", "line": 11, "user": {"login": "github-actions[bot]"}, "body": root_body},
        {"in_reply_to_id": 5, "user": {"login": "sam"}, "body": "This is intended: the caller validates x."}])
    return calls


def _fake_model(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> list[str]:
    prompts: list[str] = []

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        prompts.append(messages[1]["content"])
        return _resp(content=json.dumps(answer))

    monkeypatch.setattr(model, "complete", fake_complete)
    return prompts


def _run(req: respond.Request, repo: Path) -> str:
    return asyncio.run(respond.answer(req, PR, Change("PR #7", {"a.py": DIFF}), repo, Config(), "m", "tok",
                                      None, repo))


def test_agreeing_reply_marks_the_thread_dismissed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_github(monkeypatch, FINDING_BODY)
    prompts = _fake_model(monkeypatch, {"verdict": "agree", "reply": "You're right, @sam: x is validated."})
    msg = _run(respond.Request("reply", 7, "intended", 11, 5), repo)
    assert msg == "replied in thread (agree)"
    assert "caller validates x" in prompts[0] and "<finding>" in prompts[0]
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/pulls/7/comments/5/replies")
    assert "@sam" not in body["body"] and github._markers(body["body"])[-1] == {"dismissed": True}


def test_disagreeing_reply_does_not_dismiss(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_github(monkeypatch, FINDING_BODY)
    _fake_model(monkeypatch, {"verdict": "disagree", "reply": "The other caller in b.py doesn't validate."})
    _run(respond.Request("reply", 7, "intended", 11, 5), repo)
    assert not any(m.get("dismissed") for m in github._markers(calls[-1][2]["body"]))


def test_replies_on_threads_duhview_did_not_start_are_ignored(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_github(monkeypatch, "a human's code comment")
    assert _run(respond.Request("reply", 7, "ok", 11, 5), repo) == "not a duhview thread; ignored"
    assert all(m == "GET" for m, _, _ in calls)


def test_explain_in_the_conversation_quotes_the_question(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_github(monkeypatch, FINDING_BODY)
    prompts = _fake_model(monkeypatch, {"reply": "It caches per process (a.py:11)."})
    assert _run(respond.Request("explain", 7, "why a global cache?", 11), repo) == "answered"
    assert "why a global cache?" in prompts[0]
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues/7/comments")
    assert body["body"].startswith("> why a global cache?\n\nIt caches per process")
    # It saw the earlier thread, where the maintainer explained the code was intended.
    assert "<discussion>" in prompts[0] and "caller validates x" in prompts[0]


# --- dismissed threads ---------------------------------------------------------------------------


def test_dismissed_and_resolved_threads_stop_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    dismissed = _t(11, tid="D")
    dismissed.dismissed = True
    resolved = _t(30, resolved=True, tid="R")
    open_one = _t(20, tid="O")
    plan = github.plan_sync(_result(_f(11, 11), _f(30, 30), _f(20, 20)), CHANGE, [dismissed, resolved, open_one], [])
    assert plan.new == [] and len(plan.closed) == 2 and plan.open == 1

    monkeypatch.setattr(github, "existing_threads", lambda pr, tok: [dismissed, resolved, open_one])
    monkeypatch.setattr(github, "listed_findings", lambda pr, tok: [])
    monkeypatch.setattr(github, "mark_touched", lambda root, head, threads, remote="origin": None)
    monkeypatch.setattr(github, "upsert_summary", lambda *a: "summary updated")
    replies: list[str] = []
    monkeypatch.setattr(github, "api", lambda m, p, t, b=None: replies.append(p) or {})
    live = github.sync_review(PR, _result(_f(11, 11), _f(30, 30), _sev20()), CHANGE, "tok", Path(".")).live
    assert [(f.start_line, f.severity) for f in live] == [(20, Severity.critical)]
    assert replies == ["/repos/o/r/pulls/7/comments/100/replies"]  # only the open thread got worse


def _sev20() -> Any:
    return _f(20, 20).model_copy(update={"severity": Severity.critical})
