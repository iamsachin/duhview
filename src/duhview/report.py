from duhview.types import ReviewResult, Severity

EXIT_CLEAN, EXIT_FINDINGS, EXIT_INCOMPLETE, EXIT_ERROR = 0, 1, 2, 3


def exit_code(result: ReviewResult, block_on: Severity) -> int:
    if result.incomplete:
        return EXIT_INCOMPLETE
    if any(f.severity.rank <= block_on.rank for f in result.findings):
        return EXIT_FINDINGS
    return EXIT_CLEAN


def _cost(r: ReviewResult) -> str:
    return f"${r.cost_usd:.4f}" + ("" if r.cost_known else " (some calls unpriced)")


def to_markdown(r: ReviewResult) -> str:
    out = [f"# duhview review: {r.target}", "",
           f"Model: `{r.model}` · Verifier: `{r.verifier_model}` · Cost: {_cost(r)}", ""]
    if r.incomplete:
        out += ["## ⚠️ Review incomplete", *(f"- {i}" for i in r.incomplete), ""]
    if not r.findings:
        out.append("No findings.")
    for f in r.findings:
        lines = f"{f.start_line}" if f.start_line == f.end_line else f"{f.start_line}-{f.end_line}"
        out += [f"## [{f.severity}] {f.title}",
                f"`{f.file}:{lines}` · {f.category} · "
                + (f"verified ({f.confidence:.0%})" if f.verified else "unverified"),
                "", f.explanation, ""]
        if f.suggestion:
            out += ["Suggested change:", "```", f.suggestion, "```", ""]
    return "\n".join(out)


def receipt(r: ReviewResult, report_path: str) -> str:
    verdict = "INCOMPLETE" if r.incomplete else ("ISSUES FOUND" if r.findings else "CLEAN")
    out = [f"duhview: {verdict} — {r.target} · {len(r.findings)} finding(s) · {_cost(r)}"]
    out += [f"  [{f.severity:>8}] {f.file}:{f.start_line}  {f.title}" for f in r.findings]
    out += [f"  ! {i}" for i in r.incomplete]
    out.append(f"  report: {report_path}")
    return "\n".join(out)
