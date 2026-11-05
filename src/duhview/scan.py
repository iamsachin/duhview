"""Deterministic scanners that run before the agent.

Each scanner is optional. Only findings on lines the change adds are kept, so pre-existing
issues in touched files are not reported.
"""

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from duhview.collect import Change, added_lines
from duhview.config import Config
from duhview.types import Category, FindingDraft, Severity

SCAN_TIMEOUT_S = 180
LOCAL_SEMGREP_CONFIGS = [".semgrep.yml", ".semgrep.yaml", ".semgrep"]


class ScanSkipped(Exception):
    pass


class ScanError(Exception):
    pass


@dataclass
class ScanResult:
    findings: list[tuple[str, FindingDraft]] = field(default_factory=list)  # (scanner, finding)
    notes: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)


def _run(args: list[str], cwd: Path, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, timeout=SCAN_TIMEOUT_S, check=False)


def gitleaks(root: Path, files: list[str], offline: bool) -> list[FindingDraft]:
    drafts = []
    for f in files:
        # stdin keeps line numbers relative to this one file; --redact keeps secrets out of output.
        r = _run(["gitleaks", "stdin", "--no-banner", "--redact", "--exit-code", "0",
                  "--report-format", "json", "--report-path", "-"], root, (root / f).read_bytes())
        if r.returncode != 0:
            raise ScanError(r.stderr.decode(errors="replace").strip()[-300:])
        for leak in json.loads(r.stdout or b"[]"):
            drafts.append(FindingDraft(
                file=f, start_line=leak["StartLine"], end_line=leak["EndLine"],
                severity=Severity.critical, category=Category.security,
                title=f"Possible secret: {leak.get('Description') or leak['RuleID']}",
                explanation=f"gitleaks rule `{leak['RuleID']}` matched here. If this is a real credential, "
                            "remove it, rotate it, and load it from the environment or a secret store.",
            ))
    return drafts


_SEMGREP_SEVERITY = {"ERROR": Severity.major, "WARNING": Severity.minor, "INFO": Severity.info}


def semgrep(root: Path, files: list[str], offline: bool) -> list[FindingDraft]:
    local = next((c for c in LOCAL_SEMGREP_CONFIGS if (root / c).exists()), None)
    if local is None and offline:
        raise ScanSkipped("needs the rule registry (offline, and no local .semgrep.yml)")
    config = local or "p/default"
    r = _run(["semgrep", "scan", "--config", config, "--json", "--quiet", "--metrics", "off",
              "--disable-version-check", "--", *files], root)
    if r.returncode not in (0, 1):  # 1 = findings present
        raise ScanError(r.stderr.decode(errors="replace").strip()[-300:])
    drafts = []
    for res in json.loads(r.stdout)["results"]:
        extra = res.get("extra", {})
        meta = extra.get("metadata", {})
        security = meta.get("category") == "security" or ".security." in res["check_id"]
        drafts.append(FindingDraft(
            file=res["path"], start_line=res["start"]["line"], end_line=res["end"]["line"],
            severity=_SEMGREP_SEVERITY.get(extra.get("severity", ""), Severity.minor),
            category=Category.security if security else Category.reliability,
            title=extra.get("message", res["check_id"]).splitlines()[0][:150],
            explanation=f"semgrep rule `{res['check_id']}`: {extra.get('message', '')}",
        ))
    return drafts


SCANNERS: dict[str, Callable[[Path, list[str], bool], list[FindingDraft]]] = {
    "gitleaks": gitleaks, "semgrep": semgrep,
}


def run_scanners(root: Path, change: Change, cfg: Config, offline: bool) -> ScanResult:
    out = ScanResult()
    files = [f for f in change.files if (root / f).is_file()]  # skip deleted files
    if not files:
        return out
    explicit = cfg.scanners is not None
    for name in cfg.scanners if cfg.scanners is not None else list(SCANNERS):
        if shutil.which(name) is None:
            # Required scanners missing make the review incomplete; optional ones are just noted.
            (out.incomplete if explicit else out.notes).append(f"{name} is not installed; skipped")
            continue
        try:
            drafts = SCANNERS[name](root, files, offline)
        except ScanSkipped as e:
            out.notes.append(f"{name} skipped: {e}")
            continue
        except (ScanError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, OSError) as e:
            out.incomplete.append(f"{name} failed: {type(e).__name__}: {e}")
            continue
        for d in drafts:
            changed = added_lines(change.files.get(d.file, ""))
            if any(line in changed for line in range(d.start_line, d.end_line + 1)):
                out.findings.append((name, d))
    return out
