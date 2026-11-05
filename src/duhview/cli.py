import asyncio
from pathlib import Path
from typing import Annotated

import typer

from duhview import __version__
from duhview.collect import GitError, Target, get_change, repo_root
from duhview.config import Config, load_config
from duhview.model import ModelError, resolve_model
from duhview.report import EXIT_CLEAN, EXIT_ERROR, exit_code, receipt, to_markdown
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
    model: Annotated[str | None, typer.Option(help="Model name, e.g. claude-sonnet-5-5 or gpt-5.5.")] = None,
    verifier_model: Annotated[str | None, typer.Option(help="Enable the verify pass with this model.")] = None,
    spec: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, help="Requirements to check against.")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Review uncommitted changes (default), staged changes, a branch or a commit.

    Exit codes: 0 clean, 1 findings at or above block_on_severity, 2 incomplete review, 3 error.
    """
    if sum([staged, branch is not None, commit is not None]) > 1:
        typer.echo("error: use only one of --staged, --branch, --commit", err=True)
        raise typer.Exit(EXIT_ERROR)
    try:
        root = repo_root(Path.cwd())
        cfg = load_config(root)
        finder = resolve_model(model or cfg.model.name)
        verifier_name = verifier_model or cfg.model.verifier
        verifier = resolve_model(verifier_name) if verifier_name else None
        change = get_change(root, Target(staged, branch, commit), cfg)
    except (GitError, ModelError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EXIT_ERROR) from e

    if not change.files:
        typer.echo(f"duhview: nothing to review in {change.target}")
        raise typer.Exit(EXIT_CLEAN)

    if not json_out:
        typer.echo(f"duhview: reviewing {len(change.files)} file(s) in {change.target} with {finder}…", err=True)
    result = asyncio.run(run_review(root, change, cfg, finder, verifier, spec))
    report_path = Path(result.archive_dir or ".") / "report.md"
    report_path.write_text(to_markdown(result))
    typer.echo(result.model_dump_json(indent=2) if json_out else receipt(result, str(report_path)))
    raise typer.Exit(exit_code(result, cfg.review.block_on_severity))


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
