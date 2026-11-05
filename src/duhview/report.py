import json
from pathlib import Path
from typing import Any

from duhview import __version__
from duhview.types import Finding, ReviewResult, Severity

EXIT_CLEAN, EXIT_FINDINGS, EXIT_INCOMPLETE, EXIT_ERROR = 0, 1, 2, 3


def exit_code(result: ReviewResult, block_on: Severity) -> int:
    if result.incomplete:
        return EXIT_INCOMPLETE
    if any(f.severity.rank < block_on.rank for f in result.findings):
        return EXIT_FINDINGS
    return EXIT_CLEAN


def _cost(r: ReviewResult) -> str:
    return f"${r.cost_usd:.4f}" + ("" if r.cost_known else " (some calls unpriced)")


def _provenance(f: Finding) -> str:
    if f.source != "agent":
        return f.source
    return f"verified ({f.confidence:.0%})" if f.verified else "agent"


def to_markdown(r: ReviewResult) -> str:
    header = (f"Model: `{r.model}` · Verifier: `{r.verifier_model or 'off'}` · "
              f"Review units: {r.units} · Cost: {_cost(r)}")
    out = [f"# duhview review: {r.target}", "", header, ""]
    if r.incomplete:
        out += ["## ⚠️ Review incomplete", *(f"- {i}" for i in r.incomplete), ""]
    if r.notes:
        out += ["Notes:", *(f"- {n}" for n in r.notes), ""]
    if not r.findings:
        out.append("No findings.")
    for f in r.findings:
        lines = f"{f.start_line}" if f.start_line == f.end_line else f"{f.start_line}-{f.end_line}"
        out += [f"## [{f.severity}] {f.title}", f"`{f.file}:{lines}` · {f.category} · {_provenance(f)}",
                "", f.explanation, ""]
        if f.suggestion:
            out += ["Suggested change:", "```", f.suggestion, "```", ""]
    return "\n".join(out)


def receipt(r: ReviewResult, report_path: str) -> str:
    verdict = "INCOMPLETE" if r.incomplete else ("ISSUES FOUND" if r.findings else "CLEAN")
    out = [f"duhview: {verdict} — {r.target} · {len(r.findings)} finding(s) · {_cost(r)}"]
    out += [f"  [{f.severity:>8}] {f.file}:{f.start_line}  {f.title}" for f in r.findings]
    out += [f"  ! {i}" for i in r.incomplete]
    out += [f"  · {n}" for n in r.notes]
    out.append(f"  report: {report_path}")
    return "\n".join(out)


_SARIF_LEVEL = {Severity.critical: "error", Severity.major: "error", Severity.minor: "warning",
                Severity.nitpick: "note", Severity.info: "note"}


def _rule_id(f: Finding) -> str:
    return f"{f.source.replace(':', '/')}/{f.category}"


def to_sarif(r: ReviewResult) -> dict[str, Any]:
    """SARIF 2.1.0, the format GitHub code scanning and most CI tools accept."""
    rules = sorted({_rule_id(f) for f in r.findings})
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "duhview", "version": __version__,
                "rules": [{"id": rid, "shortDescription": {"text": rid.rsplit("/", 1)[-1]}} for rid in rules],
            }},
            "results": [{
                "ruleId": _rule_id(f),
                "ruleIndex": rules.index(_rule_id(f)),
                "level": _SARIF_LEVEL[f.severity],
                "message": {"text": f"{f.title}\n\n{f.explanation}"},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.file},
                    "region": {"startLine": f.start_line, "endLine": max(f.end_line, f.start_line)},
                }}],
                "partialFingerprints": {"duhview/v1": f.id},
                "properties": {"severity": str(f.severity), "source": f.source, "verified": f.verified},
            } for f in r.findings],
        }],
    }


def write_reports(r: ReviewResult, out_dir: Path) -> dict[str, Path]:
    paths = {"md": out_dir / "report.md", "json": out_dir / "report.json", "sarif": out_dir / "report.sarif"}
    paths["md"].write_text(to_markdown(r))
    paths["json"].write_text(r.model_dump_json(indent=2))
    paths["sarif"].write_text(json.dumps(to_sarif(r), indent=2))
    return paths
