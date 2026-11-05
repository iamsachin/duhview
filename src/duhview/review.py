"""The review pipeline: collect -> find -> verify -> result."""

import asyncio
from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from duhview import model as llm
from duhview.agent import ask_json
from duhview.archive import Archive
from duhview.collect import Change, project_context
from duhview.config import Config
from duhview.tools import RepoTools
from duhview.types import FinderOutput, Finding, FindingDraft, ReviewResult, Verdict

VERIFY_CONCURRENCY = 4
VERIFY_MAX_TOOL_CALLS = 15


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


def _dedupe(drafts: list[FindingDraft]) -> list[FindingDraft]:
    kept: list[FindingDraft] = []
    for d in sorted(drafts, key=lambda d: d.severity.rank):
        if not any(k.file == d.file and k.category == d.category and abs(k.start_line - d.start_line) <= 2
                   for k in kept):
            kept.append(d)
    return kept


async def run_review(root: Path, change: Change, cfg: Config, finder_model: str, verifier_model: str | None,
                     spec: Path | None) -> ReviewResult:
    """`verifier_model=None` skips the verify pass."""
    budget = llm.Budget(cfg.budget.max_usd_per_review)
    archive = Archive(root)
    tools = RepoTools(root)
    log: list[Any] = []
    incomplete: list[str] = []
    if change.truncated:
        incomplete.append("diff too large: some files were not reviewed")

    context = project_context(root, cfg, list(change.files), spec)
    base = (_fence("project_context", context) + "\n\n" if context else "") + _fence("untrusted_diff", change.diff)
    finder_sys, max_calls = _prompt("finder"), cfg.budget.max_tool_calls_per_agent

    # --- find ---
    drafts: list[FindingDraft] = []
    try:
        out = await _retry_once(lambda: ask_json(finder_model, finder_sys, base, FinderOutput, tools, budget,
                                                 max_calls, log), cfg.budget.agent_timeout_s)
        drafts = out.findings
    except llm.BudgetExceeded as e:
        incomplete.append(f"find: {e}")
    except Exception as e:  # noqa: BLE001 - any agent failure falls back to diff-only
        incomplete.append(f"agentic review failed ({type(e).__name__}: {e}); used diff-only review")
        try:
            note = "\n\nNo tools are available in this run; review from the diff alone."
            out = await asyncio.wait_for(ask_json(finder_model, finder_sys, base + note, FinderOutput, None,
                                                  budget, 0, log), cfg.budget.agent_timeout_s)
            drafts = out.findings
        except Exception as e2:  # noqa: BLE001 - report, never crash the review
            incomplete.append(f"diff-only review failed ({type(e2).__name__}: {e2})")

    # --- verify ---
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    rejected: list[dict[str, Any]] = []
    unverified = 0

    async def check(d: FindingDraft) -> Finding | None:
        nonlocal unverified
        f = Finding.from_draft(d)
        if verifier_model is None:
            return f
        vm = verifier_model
        user = base + "\n\n" + _fence("finding", d.model_dump_json(indent=2))
        async with sem:
            try:
                v = await _retry_once(lambda: ask_json(vm, _prompt("verifier"), user, Verdict, tools,
                                                       budget, VERIFY_MAX_TOOL_CALLS, log),
                                      cfg.budget.agent_timeout_s)
            except Exception:  # noqa: BLE001 - keep the finding, marked unverified
                unverified += 1
                return f  # keep it, marked unverified
        if v.verdict == "reject":
            rejected.append({"finding": f.model_dump(), "reason": v.reason})
            return None
        return f.model_copy(update={"verified": True, "severity": v.severity, "confidence": v.confidence})

    results = await asyncio.gather(*(check(d) for d in _dedupe(drafts)))
    if unverified:
        incomplete.append(f"verify: {unverified} finding(s) could not be verified")

    min_rank = cfg.review.min_severity.rank
    findings = sorted((f for f in results if f and f.severity.rank <= min_rank), key=lambda f: f.severity.rank)
    result = ReviewResult(target=change.target, model=finder_model, verifier_model=verifier_model,
                          findings=findings, incomplete=incomplete, cost_usd=round(budget.spent, 4),
                          cost_known=budget.known, archive_dir=str(archive.dir))
    archive.write("conversations.json", log)
    archive.write("rejected.json", rejected)
    archive.write("result.json", result.model_dump(mode="json"))
    return result
