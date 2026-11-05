import asyncio
import shutil
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from duhview import __version__, github
from duhview.collect import GitError, Target, get_change, repo_root, revision_reader
from duhview.config import Config, load_config
from duhview.github import GitHubError
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
    model: Annotated[str | None, typer.Option(help="Model name, e.g. claude-sonnet-5-5 or gpt-5.5.")] = None,
    verifier_model: Annotated[str | None, typer.Option(help="Enable the verify pass with this model.")] = None,
    spec: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, help="Requirements to check against.")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
    sarif: Annotated[Path | None, typer.Option(dir_okay=False, help="Also write a SARIF report here.")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Skip scanners that need the network.")] = False,
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
            if pr is not None:
                tok = github.token()
                pull = github.get_pr(*github.parse_pr_ref(pr, root), tok)
                review_root = stack.enter_context(github.pr_checkout(root, pull))
                read_trusted = revision_reader(root, pull.base_sha)
                target = Target(branch=pull.base_sha)
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
                                        read_trusted=read_trusted, archive_root=root))
        reports = write_reports(result, Path(result.archive_dir or "."))
        if sarif:
            shutil.copyfile(reports["sarif"], sarif)
        if post and pull:
            try:
                typer.echo(f"duhview: {github.sync_review(pull, result, change, tok)}", err=True)
            except GitHubError as e:
                typer.echo(receipt(result, str(reports["md"])))
                _fail(f"review finished but posting failed: {e}")
        typer.echo(result.model_dump_json(indent=2) if json_out else receipt(result, str(reports["md"])))
        raise typer.Exit(exit_code(result, cfg.review.block_on_severity))


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
