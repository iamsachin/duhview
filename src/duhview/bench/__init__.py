"""Planted-bug cases: build a throwaway repo per case, review it, score the findings.

Each case directory holds `before/` (committed), `after/` (the uncommitted change) and
`expected.yaml` (the bugs planted in `after/`, located by a unique substring of the line).
"""

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel

from duhview.collect import Target, get_change, git
from duhview.config import Config
from duhview.review import run_review
from duhview.types import Category, Finding

LINE_TOLERANCE = 3
GIT_ID = ["-c", "user.name=duhview", "-c", "user.email=duhview@localhost",
          "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


class Location(BaseModel):
    file: str
    match: str


class Bug(BaseModel):
    id: str
    category: Category
    locations: list[Location]


class CaseSpec(BaseModel):
    description: str
    bugs: list[Bug]


@dataclass
class Case:
    name: str
    dir: Path
    spec: CaseSpec
    lines: dict[str, list[tuple[str, int]]]  # bug id -> (file, line) where it may be reported


@dataclass
class Score:
    case: str
    bugs: int
    caught: list[str]
    findings: int
    true_findings: int
    cost_usd: float
    incomplete: list[str]


def cases_dir() -> Path:
    # shortcut: assumes the package is installed as files on disk, not zipped.
    return Path(str(files("duhview.bench").joinpath("cases")))


def load_cases(names: list[str] | None = None) -> list[Case]:
    cases = []
    for d in sorted(p for p in cases_dir().iterdir() if p.is_dir()):
        if names and d.name not in names:
            continue
        spec = CaseSpec.model_validate(yaml.safe_load((d / "expected.yaml").read_text()))
        lines: dict[str, list[tuple[str, int]]] = {}
        for bug in spec.bugs:
            for loc in bug.locations:
                text = (d / "after" / loc.file).read_text().splitlines()
                hits = [i for i, line in enumerate(text, 1) if loc.match in line]
                if len(hits) != 1:
                    raise ValueError(f"{d.name}/{bug.id}: '{loc.match}' matches {len(hits)} lines in {loc.file}")
                lines.setdefault(bug.id, []).append((loc.file, hits[0]))
        cases.append(Case(d.name, d, spec, lines))
    if names and len(cases) != len(set(names)):
        known = sorted(p.name for p in cases_dir().iterdir() if p.is_dir())
        raise ValueError(f"unknown case in {names}; known: {', '.join(known)}")
    return cases


def build_repo(case: Case, dest: Path) -> None:
    """Commit `before/`, then leave `after/` as the uncommitted change."""
    shutil.copytree(case.dir / "before", dest, dirs_exist_ok=True)
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, *GIT_ID, "commit", "-q", "-m", "base")
    for p in dest.iterdir():
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(case.dir / "after", dest, dirs_exist_ok=True)


def score(case: Case, findings: list[Finding]) -> tuple[list[str], int]:
    """Return (bug ids caught, number of findings that hit a planted bug)."""
    caught: set[str] = set()
    true = 0
    for f in findings:
        hit = {bug_id for bug_id, locs in case.lines.items()
               if any(file == f.file and f.start_line - LINE_TOLERANCE <= line <= f.end_line + LINE_TOLERANCE
                      for file, line in locs)}
        true += bool(hit)
        caught |= hit
    return sorted(caught), true


async def run_case(case: Case, finder: str, verifier: str, verify: bool) -> Score:
    with tempfile.TemporaryDirectory(prefix=f"duhview-{case.name}-") as tmp:
        root = Path(tmp).resolve()
        build_repo(case, root)
        cfg = Config()
        result = await run_review(root, get_change(root, Target(), cfg), cfg, finder, verifier, None, verify=verify)
    caught, true = score(case, result.findings)
    return Score(case.name, len(case.spec.bugs), caught, len(result.findings), true, result.cost_usd,
                 result.incomplete)


async def run_bench(cases: list[Case], finder: str, verifier: str, verify: bool = True,
                    concurrency: int = 4) -> list[Score]:
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> Score:
        async with sem:
            return await run_case(c, finder, verifier, verify)

    return list(await asyncio.gather(*(one(c) for c in cases)))


def totals(scores: list[Score]) -> dict[str, float]:
    bugs = sum(s.bugs for s in scores)
    findings = sum(s.findings for s in scores)
    return {
        "recall": sum(len(s.caught) for s in scores) / bugs if bugs else 1.0,
        "precision": sum(s.true_findings for s in scores) / findings if findings else 1.0,
        "false_positives": findings - sum(s.true_findings for s in scores),
        "cost_usd": round(sum(s.cost_usd for s in scores), 4),
    }


def table(scores: list[Score]) -> str:
    rows = [f"{'case':<15} {'caught':>7} {'findings':>9} {'false+':>7} {'cost':>8}"]
    for s in scores:
        rows.append(f"{s.case:<15} {len(s.caught):>3}/{s.bugs:<3} {s.findings:>9} "
                    f"{s.findings - s.true_findings:>7} {s.cost_usd:>8.4f}" + ("  (incomplete)" if s.incomplete else ""))
    t = totals(scores)
    rows.append(f"recall {t['recall']:.0%} · precision {t['precision']:.0%} · "
                f"false positives {t['false_positives']:.0f} · cost ${t['cost_usd']:.4f}")
    return "\n".join(rows)
