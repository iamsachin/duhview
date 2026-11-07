"""The review pipeline: collect -> scan -> find (per review unit) -> verify -> result."""

import asyncio
from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from duhview import model as llm
from duhview.agent import ask_json
from duhview.archive import Archive
from duhview.collect import Change, Reader, project_context, review_units
from duhview.config import Config
from duhview.scan import run_scanners
from duhview.tools import RepoTools
from duhview.types import FinderOutput, Finding, FindingDraft, PRSummary, ReviewResult, Verdict

FINDER_CONCURRENCY = 4
FINDER_BUDGET_SHARE_WITH_VERIFY = 0.7  # leave the rest for the verify pass
BUDGET_SAFETY_MARGIN = 0.75  # the final answer turn costs more than tool turns (reasoning, full context)
VERIFY_CONCURRENCY = 4
VERIFY_MAX_TOOL_CALLS = 15
MAX_DESCRIPTION_CHARS = 10_000
SUMMARY_FILE_CHARS = 20_000  # per file, so one huge file can't crowd out the rest
SUMMARY_TOTAL_CHARS = 150_000


def _prompt(name: str) -> str:
    return files("duhview.prompts").joinpath(f"{name}.md").read_text()


def _fence(tag: str, text: str) -> str:
    # Stop untrusted text from closing our delimiter early.
    return f"<{tag}>\n{text.replace(f'</{tag}>', f'</ {tag}>')}\n</{tag}>"


async def _retry_once[T](fn: Callable[[], Awaitable[T]], timeout_s: int) -> T:
    for attempt in range(2):
        try:
            return await asyncio.wait_for(fn(), timeout_s)
        except llm.BudgetExceeded:
            raise
        except Exception:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


SAME_MODEL_LINES = 2
CROSS_MODEL_LINES = 5  # different models cite the same bug a few lines apart (seen live: 376 vs 381)


def _same_spot(a: Finding, b: Finding) -> bool:
    if a.file != b.file or a.category != b.category:
        return False
    if a.models and b.models and not set(a.models) & set(b.models):
        return a.start_line - CROSS_MODEL_LINES <= b.end_line and b.start_line - CROSS_MODEL_LINES <= a.end_line
    return abs(a.start_line - b.start_line) <= SAME_MODEL_LINES


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Keep the most severe of findings on the same spot; on ties, the earlier one. The kept finding
    records every finder model that raised the same spot."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda f: f.severity.rank):  # stable sort keeps input order on ties
        k = next((k for k in kept if _same_spot(k, f)), None)
        if k is None:
            kept.append(f.model_copy(update={"models": list(f.models)}))
        else:
            k.models += [m for m in f.models if m not in k.models]
    return kept


async def _summarize(change: Change, model: str, budget: llm.Budget, log: list[Any], timeout: int) -> PRSummary:
    parts, size = [], 0
    for path in sorted(change.files):
        d = change.files[path]
        if size + min(len(d), SUMMARY_FILE_CHARS) > SUMMARY_TOTAL_CHARS:
            parts.append(f"diff --git a/{path} b/{path}\n(diff omitted for length)\n")
            continue
        parts.append(d[:SUMMARY_FILE_CHARS] + ("\n... (file diff truncated)\n" if len(d) > SUMMARY_FILE_CHARS else ""))
        size += min(len(d), SUMMARY_FILE_CHARS)
    user = _fence("untrusted_diff", "".join(parts))
    return await asyncio.wait_for(ask_json(model, _prompt("summary"), user, PRSummary, None, budget, 0, log), timeout)


def _label(unit: dict[str, str]) -> str:
    names = list(unit)
    return names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} files)"


async def run_review(root: Path, change: Change, cfg: Config, finder_model: str, verifier_model: str | None,
                     spec: Path | None, offline: bool = False, read_trusted: Reader | None = None,
                     archive_root: Path | None = None, summarize: bool = False,
                     focus: str | None = None, description: str | None = None,
                     known: str | None = None) -> ReviewResult:
    """`verifier_model=None` skips the verify pass. `read_trusted` supplies team rules and the
    false-positive filter (the PR base in PR mode); `archive_root` is where runs are saved.
    `summarize` also writes a short summary of the change for human reviewers."""
    budget = llm.Budget(cfg.budget.max_usd_per_review)
    archive = Archive(archive_root or root)
    tools = RepoTools(root)
    log: list[Any] = []
    incomplete = [f"{f}: diff too large, only the first part was reviewed" for f in change.truncated]

    # --- scan ---
    scan = await asyncio.to_thread(run_scanners, root, change, cfg, offline or cfg.offline)
    notes, incomplete = scan.notes, incomplete + scan.incomplete
    scanner_findings = [Finding.from_draft(d, source=f"scanner:{name}") for name, d in scan.findings]

    context = project_context(root, cfg, list(change.files), spec, read_trusted)
    ctx = _fence("project_context", context) + "\n\n" if context else ""
    if description:  # the PR's title and body: what the change claims to do (untrusted, author-written)
        ctx += _fence("pull_request_description", description[:MAX_DESCRIPTION_CHARS]) + "\n\n"
    if known:  # duhview's own open findings on this PR (seen live: restated in new words on every push)
        ctx += _fence("already_reported", "duhview already reported these on this pull request:\n" + known
                      + "\n\nIf one of them still exists, report it again with exactly the same title and its current"
                      " lines; never restate it in other words. Report a different problem only if it is not"
                      " covered by this list.") + "\n\n"
    if focus:  # from a maintainer's `@duhview review <focus>`: a request to look harder, not to look less
        ctx += _fence("review_request", f"A maintainer asked you to pay particular attention to: {focus}") + "\n\n"
    finder_sys, max_calls, timeout = _prompt("finder"), cfg.budget.max_tool_calls_per_agent, cfg.budget.agent_timeout_s

    # --- find: one agent per review unit, in parallel, each with a share of the budget ---
    units = review_units(change)
    pool = (cfg.budget.max_usd_per_review * BUDGET_SAFETY_MARGIN
            * (FINDER_BUDGET_SHARE_WITH_VERIFY if verifier_model else 1.0))
    finders = finder_model.split(",")  # several models: each reviews every unit; findings are merged
    share = pool / (max(len(units), 1) * len(finders))
    find_sem = asyncio.Semaphore(FINDER_CONCURRENCY)

    async def find(unit: dict[str, str], fm: str) -> list[Finding]:
        return [Finding.from_draft(d).model_copy(update={"models": [fm]}) for d in await find_drafts(unit, fm)]

    async def find_drafts(unit: dict[str, str], finder_model: str) -> list[FindingDraft]:
        parts = [ctx]
        leads = [f"- {f.file}:{f.start_line} {f.title}" for f in scanner_findings if f.file in unit]
        if leads:
            parts.append(_fence("scanner_results", "Static scanners already reported these; do not repeat "
                                "them, but use them as leads:\n" + "\n".join(leads)) + "\n\n")
        others = sorted(set(change.files) - set(unit))
        if others:
            parts.append("Other files in this change are reviewed separately; read them if relevant: "
                         + ", ".join(others[:200]) + "\n\n")
        parts.append(_fence("untrusted_diff", "".join(unit.values())))
        user, label = "".join(parts), _label(unit) + (f" [{finder_model}]" if len(finders) > 1 else "")
        async with find_sem:
            try:
                out = await _retry_once(lambda: ask_json(finder_model, finder_sys, user, FinderOutput, tools,
                                                         budget, max_calls, log, share), timeout)
                return out.findings
            except llm.BudgetExceeded as e:
                incomplete.append(f"{label}: not reviewed, {e}")
                return []
            except Exception as e:  # noqa: BLE001 - any agent failure falls back to diff-only
                incomplete.append(f"{label}: agentic review failed ({type(e).__name__}: {e}); used diff-only review")
            try:
                note = "\n\nNo tools are available in this run; review from the diff alone."
                out = await asyncio.wait_for(ask_json(finder_model, finder_sys, user + note, FinderOutput, None,
                                                      budget, 0, log), timeout)
                return out.findings
            except Exception as e2:  # noqa: BLE001 - report, never crash the review
                incomplete.append(f"{label}: diff-only review failed ({type(e2).__name__}: {e2})")
                return []

    # The summary needs no tools, so it runs alongside the finders.
    summary_task = asyncio.create_task(_summarize(change, finders[0], budget, log, timeout)) if summarize else None
    found = [f for fs in await asyncio.gather(*(find(u, fm) for u in units for fm in finders)) for f in fs]
    # Agent findings first so, on ties, the agent's fuller explanation wins over the scanner's.
    candidates = _dedupe(found + scanner_findings)

    # --- verify (agent findings only; scanner findings are deterministic) ---
    verify_sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    rejected: list[dict[str, Any]] = []
    unverified = 0

    async def check(f: Finding) -> Finding | None:
        nonlocal unverified
        if verifier_model is None or f.source != "agent":
            return f
        vm = verifier_model
        user = (ctx + _fence("untrusted_diff", change.files.get(f.file, "(file not in this change)")) + "\n\n"
                + _fence("finding", FindingDraft.model_validate(f.model_dump()).model_dump_json(indent=2)))
        async with verify_sem:
            try:
                v = await _retry_once(lambda: ask_json(vm, _prompt("verifier"), user, Verdict, tools,
                                                       budget, VERIFY_MAX_TOOL_CALLS, log), timeout)
            except Exception:  # noqa: BLE001 - keep the finding, marked unverified
                unverified += 1
                return f
        if v.verdict == "reject":
            rejected.append({"finding": f.model_dump(), "reason": v.reason})
            return None
        return f.model_copy(update={"verified": True, "severity": v.severity, "confidence": v.confidence})

    results = await asyncio.gather(*(check(f) for f in candidates))
    if unverified:
        incomplete.append(f"verify: {unverified} finding(s) could not be verified")

    min_rank = cfg.review.min_severity.rank
    # Within a severity, findings several models agree on come first.
    findings = sorted((f for f in results if f and f.severity.rank <= min_rank),
                      key=lambda f: (f.severity.rank, -len(f.models)))
    summary = None
    if summary_task is not None:
        try:
            summary = await summary_task
        except Exception as e:  # noqa: BLE001 - a summary is a nice-to-have; never fail the review
            notes.append(f"summary unavailable ({type(e).__name__})")
    result = ReviewResult(target=change.target, model=finder_model, verifier_model=verifier_model,
                          findings=findings, incomplete=incomplete, notes=notes, units=len(units), summary=summary,
                          cost_usd=round(budget.spent, 4), cost_known=budget.known, archive_dir=str(archive.dir))
    archive.write("conversations.json", log)
    archive.write("rejected.json", rejected)
    archive.write("result.json", result.model_dump(mode="json"))
    return result
