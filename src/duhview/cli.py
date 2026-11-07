import asyncio
import json
import os
import shutil
import sys
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from duhview import __version__, github
from duhview.collect import GitError, Target, get_change, repo_root, revision_reader, temp_worktree
from duhview.config import Config, load_config
from duhview.github import GitHubError
from duhview.hooks import BYPASS_ENV, install_hook, push_ranges, record_bypass
from duhview.model import ModelError, resolve_model
from duhview.report import EXIT_CLEAN, EXIT_ERROR, exit_code, receipt, write_reports
from duhview.review import run_review

app = typer.Typer(add_completion=False, help="Agentic code review for your changes.")


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False) -> None:
    pass


@app.command()
def review(
    staged: Annotated[bool, typer.Option("--staged", help="Review staged changes only.")] = False,
    branch: Annotated[str | None, typer.Option(help="Review BRANCH...HEAD.")] = None,
    commit: Annotated[str | None, typer.Option(help="Review a single commit.")] = None,
    pr: Annotated[str | None, typer.Option(help="Review a GitHub pull request (URL or number).")] = None,
    post: Annotated[bool, typer.Option("--post", help="Post the result as a review on the --pr.")] = False,
    resolve: Annotated[bool, typer.Option(help="With --post, resolve threads whose finding was fixed.")] = True,
    model: Annotated[str | None, typer.Option(help="Model name, e.g. claude-sonnet-5-5 or gpt-5.5.")] = None,
    verifier_model: Annotated[str | None, typer.Option(help="Enable the verify pass with this model.")] = None,
    spec: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, help="Requirements to check against.")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
    sarif: Annotated[Path | None, typer.Option(dir_okay=False, help="Also write a SARIF report here.")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Skip scanners that need the network.")] = False,
    focus: Annotated[str | None, typer.Option(hidden=True, help="Extra focus requested by a maintainer.")] = None,
) -> None:
    """Review uncommitted changes (default), staged changes, a branch, a commit or a pull request.

    With --pr, team config, rules and the false-positive filter come from the PR's base branch,
    so a pull request cannot weaken its own review.

    Exit codes: 0 clean, 1 findings at or above block_on_severity, 2 incomplete review, 3 error.
    """
    if sum([staged, branch is not None, commit is not None, pr is not None]) > 1:
        _fail("use only one of --staged, --branch, --commit, --pr")
    if post and pr is None:
        _fail("--post needs --pr")
    with ExitStack() as stack:
        try:
            root = repo_root(Path.cwd())
            review_root, target, read_trusted, pull, tok = root, Target(staged, branch, commit), None, None, ""
            known = ""
            if pr is not None:
                tok = github.token()
                pull = github.get_pr(*github.parse_pr_ref(pr, root), tok)
                review_root = stack.enter_context(github.pr_checkout(root, pull))
                read_trusted = revision_reader(root, pull.base_sha)
                target = Target(branch=pull.base_sha)
                if post:
                    known = github.known_issues(pull, tok)
            cfg = load_config(review_root, read_trusted)
            finder = resolve_model(model or cfg.model.name)
            verifier_name = verifier_model or cfg.model.verifier
            verifier = resolve_model(verifier_name) if verifier_name else None
            change = get_change(review_root, target, cfg)
            if pull:
                change.target = f"PR #{pull.number}"
        except (GitError, GitHubError, ModelError, ValueError) as e:
            _fail(str(e))

        if not change.files:
            typer.echo(f"duhview: nothing to review in {change.target}")
            raise typer.Exit(EXIT_CLEAN)
        if not json_out:
            typer.echo(f"duhview: reviewing {len(change.files)} file(s) in {change.target} with {finder}…", err=True)
        result = asyncio.run(run_review(review_root, change, cfg, finder, verifier, spec, offline,
                                        read_trusted=read_trusted, archive_root=root, summarize=post,
                                        focus=focus,
                                        description=f"# {pull.title}\n\n{pull.body}" if pull else None,
                                        known=known or None))
        reports = write_reports(result, Path(result.archive_dir or "."))
        if sarif:
            shutil.copyfile(reports["sarif"], sarif)
        if post and pull:
            try:
                synced = github.sync_review(pull, result, change, tok, review_root, resolve)
            except GitHubError as e:
                typer.echo(receipt(result, str(reports["md"])))
                _fail(f"review finished but posting failed: {e}")
            typer.echo(f"duhview: {synced.message}", err=True)
            # Findings on threads a person resolved, or that duhview agreed were wrong, don't fail the check.
            result = result.model_copy(update={"findings": synced.live})
        typer.echo(result.model_dump_json(indent=2) if json_out else receipt(result, str(reports["md"])))
        raise typer.Exit(exit_code(result, cfg.review.block_on_severity))


@app.command(hidden=True)
def respond(
    event: Annotated[Path | None, typer.Option(help="Event payload (default: $GITHUB_EVENT_PATH).")] = None,
    name: Annotated[str | None, typer.Option(help="Event name (default: $GITHUB_EVENT_NAME).")] = None,
    resolve: Annotated[bool, typer.Option(help="For @duhview review: resolve fixed threads.")] = True,
    model: Annotated[str | None, typer.Option(help="Model name (default: base branch config).")] = None,
) -> None:
    """Handle a PR comment event: `@duhview review`, `@duhview explain`, or a reply on a duhview thread."""
    from duhview.respond import answer, parse_event

    path = event or Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    req = parse_event(name or os.environ.get("GITHUB_EVENT_NAME", ""), json.loads(path.read_text()))
    if req is None:
        typer.echo("duhview: nothing to do for this comment")
        raise typer.Exit(EXIT_CLEAN)
    if req.kind == "review":
        review(pr=str(req.pr_number), post=True, resolve=resolve, focus=req.text or None, model=model)
    with ExitStack() as stack:
        try:
            root = repo_root(Path.cwd())
            tok = github.token()
            pull = github.get_pr(*github.parse_pr_ref(str(req.pr_number), root), tok)
            wt = stack.enter_context(github.pr_checkout(root, pull))
            read_trusted = revision_reader(root, pull.base_sha)
            cfg = load_config(wt, read_trusted)
            finder = resolve_model(model or cfg.model.name)
            change = get_change(wt, Target(branch=pull.base_sha), cfg)
            message = asyncio.run(answer(req, pull, change, wt, cfg, finder, tok, read_trusted, root))
        except (GitError, GitHubError, ModelError, ValueError) as e:
            _fail(str(e))
    typer.echo(f"duhview: {message}")


@app.command(hidden=True)
def reviewbench() -> None:
    """Entry point for the ReviewBench container (reads RB_* variables)."""
    from duhview.reviewbench import main as rb_main

    raise typer.Exit(rb_main())


@app.command("hook-run", hidden=True)
def hook_run(
    remote: Annotated[str, typer.Argument()] = "origin",
    url: Annotated[str, typer.Argument()] = "",
) -> None:
    """Run by the pre-push hook: review the commits being pushed.

    Blocks the push (exit 1) only on findings at or above block_on_severity. An incomplete
    review or a setup problem warns and lets the push through, so duhview never strands you.
    """
    stdin = sys.stdin.read()
    try:
        root = repo_root(Path.cwd())
        ranges = push_ranges(root, remote, stdin)
    except GitError as e:
        typer.echo(f"duhview: not reviewed ({e}); letting the push through", err=True)
        raise typer.Exit(0) from e
    if not ranges:
        raise typer.Exit(0)

    mode = os.environ.get(BYPASS_ENV, "").strip().lower()
    if mode in ("skip", "vouch"):
        where = record_bypass(root, mode, ranges)
        typer.echo(f"duhview: {mode} recorded in {where}", err=True)
        raise typer.Exit(0)
    if mode:
        typer.echo(f"duhview: ignoring {BYPASS_ENV}={mode} (use skip or vouch)", err=True)

    blocked = False
    for r in ranges:
        with temp_worktree(root, r.head, prefix="duhview-push-") as wt:
            try:
                cfg = load_config(wt)
                finder = resolve_model(cfg.model.name)
                verifier = resolve_model(cfg.model.verifier) if cfg.model.verifier else None
                change = get_change(wt, Target(base=r.base), cfg)
            except (GitError, ModelError, ValueError) as e:
                typer.echo(f"duhview: {r.ref} not reviewed ({e}); letting the push through", err=True)
                continue
            if not change.files:
                continue
            change.target = f"push to {r.ref}"
            typer.echo(f"duhview: reviewing {len(change.files)} file(s) pushed to {r.ref} with {finder}…", err=True)
            result = asyncio.run(run_review(wt, change, cfg, finder, verifier, None, archive_root=root))
        reports = write_reports(result, Path(result.archive_dir or "."))
        typer.echo(receipt(result, str(reports["md"])), err=True)
        # Serious findings block even when another part of the review was incomplete.
        if any(f.severity.rank <= cfg.review.block_on_severity.rank for f in result.findings):
            blocked = True
        elif result.incomplete:
            typer.echo("duhview: review incomplete; letting the push through", err=True)
    if blocked:
        typer.echo(f"duhview: push blocked. Fix the findings, or push anyway (recorded): "
                   f"{BYPASS_ENV}=skip git push, or {BYPASS_ENV}=vouch git push if you reviewed it yourself.",
                   err=True)
        raise typer.Exit(1)


@app.command("install-hook")
def install_hook_cmd(
    force: Annotated[bool, typer.Option("--force", help="Replace an existing pre-push hook (kept as .bak).")] = False,
) -> None:
    """Install a git pre-push hook that reviews the commits you push."""
    try:
        path = install_hook(repo_root(Path.cwd()), force)
    except (GitError, ValueError) as e:
        _fail(str(e))
    typer.echo(f"duhview: installed {path}. Pushes are now reviewed; "
               f"bypass once with {BYPASS_ENV}=skip or {BYPASS_ENV}=vouch (both recorded).")


@app.command("install-skill")
def install_skill(
    path: Annotated[Path | None, typer.Option(file_okay=False, help="Skills directory to install into.")] = None,
) -> None:
    """Install the duhview skill for coding agents (Claude Code, Codex)."""
    homes = [path] if path else [d / "skills" for d in (Path.home() / ".claude", Path.home() / ".codex") if d.is_dir()]
    if not homes:
        _fail("no ~/.claude or ~/.codex found; pass --path <skills directory>")
    text = files("duhview").joinpath("skill/SKILL.md").read_text()
    for home in homes:
        target = home / "duhview" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        typer.echo(f"duhview: installed skill {target}")


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(EXIT_ERROR)


@app.command()
def demo(
    case: Annotated[str, typer.Option(help="Case name, comma-separated names, or 'all'.")] = "py_cross_file",
    model: Annotated[str | None, typer.Option(help="Model name, e.g. claude-sonnet-5-5 or gpt-5.5.")] = None,
    verifier_model: Annotated[str | None, typer.Option(help="Enable the verify pass with this model.")] = None,
) -> None:
    """Review built-in repos with planted bugs and show how many duhview caught."""
    from duhview.bench import load_cases, run_bench, table

    try:
        cases = load_cases(None if case == "all" else case.split(","))
        name = model or Config().model.name
        finder = resolve_model(name)
        verifier = resolve_model(verifier_model) if verifier_model else None
    except (ModelError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_ERROR) from e
    typer.echo(f"duhview demo: {len(cases)} case(s) with {finder}…", err=True)
    typer.echo(table(asyncio.run(run_bench(cases, finder, verifier))))
