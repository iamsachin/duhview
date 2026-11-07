"""Recall and precision on small ReviewBench test-set PRs, scored by a small LLM judge.

    .venv/bin/python eval/reviewbench_eval.py --label baseline [--max-lines 500] [--cap 5] [--pr-context]

Fetches the public test set and its human-checked ("golden") findings, reviews each PR with
duhview, and asks a judge model which golden findings duhview caught. Not the official judge:
numbers are for comparing duhview against itself. Spend is tracked and the run stops before
crossing --cap. Results are appended to eval/reviewbench_results.jsonl.
"""

import argparse
import asyncio
import json
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from duhview import model as llm
from duhview.agent import ask_json
from duhview.collect import Change, split_diff
from duhview.config import BudgetCfg, Config, ReviewCfg
from duhview.model import resolve_model
from duhview.review import run_review
from duhview.types import Finding, Severity

RAW = "https://raw.githubusercontent.com/review-bench/ReviewBench/main"
HERE = Path(__file__).parent
CACHE = HERE / ".cache" / "reviewbench"
RESULTS = HERE / "reviewbench_results.jsonl"
SERIOUS = {"critical", "high", "medium"}

JUDGE = """You compare two lists of code review findings on the same pull request.
GOLDEN lists issues confirmed by experts. CANDIDATE lists a reviewer's findings.
A candidate matches a golden issue when it describes the same underlying problem (same root
cause), even if worded differently or a few lines off. Same file or line alone is not a match.
Each golden issue matches at most one candidate, and each candidate at most one golden issue.
Reply with only JSON: {"matches": [{"golden": "g1", "candidate": "c3"}]}"""


class Match(BaseModel):
    golden: str
    candidate: str


class Matches(BaseModel):
    matches: list[Match]


def fetch(path: str) -> bytes:
    cached = CACHE / "files" / path
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{RAW}/{path}", timeout=60) as r:
            cached.write_bytes(r.read())
    return cached.read_bytes()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={"GIT_LFS_SKIP_SMUDGE": "1", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                               "HOME": str(Path.home())}).stdout


def checkout(task: dict[str, Any]) -> tuple[Path, str]:
    """A checkout at head (from the benchmark's mirror, like its harness) and the three-dot diff."""
    name = task["nwo"].replace("/", "_")
    repo = CACHE / "repos" / name
    if not (repo / ".git").exists():
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "remote", "add", "origin", f"https://github.com/review-bench/{name}.git")
    git(repo, "fetch", "-q", "--filter=blob:none", "origin", task["base"], task["head"])
    git(repo, "checkout", "-q", "--force", "--detach", task["head"])
    git(repo, "clean", "-qfdx")
    return repo, git(repo, "diff", f"{task['base']}...{task['head']}")


async def judge(golden: list[dict[str, Any]], found: list[Finding], judge_model: str,
                budget: llm.Budget) -> set[tuple[str, str]]:
    if not golden or not found:
        return set()
    g = "\n".join(f"g{i}: [{x.get('severity')}] {x.get('file')}:{x.get('start_line')} {x.get('message', '')[:600]}"
                  for i, x in enumerate(golden))
    c = "\n".join(f"c{i}: {f.file}:{f.start_line}-{f.end_line} {f.title}. {f.explanation[:600]}"
                  for i, f in enumerate(found))
    out = await ask_json(judge_model, JUDGE, f"GOLDEN:\n{g}\n\nCANDIDATE:\n{c}", Matches, None, budget, 0, [])
    pairs, used_g, used_c = set(), set(), set()
    for m in out.matches:  # enforce one-to-one, and ignore ids the judge made up
        if m.golden not in used_g and m.candidate not in used_c and \
                m.golden[1:].isdigit() and int(m.golden[1:]) < len(golden) and \
                m.candidate[1:].isdigit() and int(m.candidate[1:]) < len(found):
            pairs.add((m.golden, m.candidate))
            used_g.add(m.golden)
            used_c.add(m.candidate)
    return pairs


async def run(args: argparse.Namespace) -> None:
    tasks = [t for t in json.loads(fetch("corpus/test/test.json"))
             if t["lines_added"] + t["lines_removed"] <= args.max_lines][: args.limit]
    finder, judge_model = resolve_model(args.model), resolve_model(args.judge)
    cfg = Config(scanners=[], budget=BudgetCfg(max_usd_per_review=args.per_review),
                 review=ReviewCfg(min_severity=Severity(args.min_severity)))
    spent = 0.0
    rows: list[dict[str, Any]] = []
    print(f"{len(tasks)} PRs <= {args.max_lines} lines · model {finder} · cap ${args.cap}")
    for t in tasks:
        if spent + args.per_review > args.cap:
            print(f"stopping: next PR could cross the ${args.cap} cap (spent ${spent:.2f})")
            break
        key = f"{t['nwo'].replace('/', '_')}_{t['pr_number']}-{t['head'][:8]}"
        golden = json.loads(fetch(f"golden/{key}.json"))
        golden = golden.get("findings", golden) if isinstance(golden, dict) else golden
        repo, diff = checkout(t)
        # The PR's title and description, as `--pr` mode gives them to the reviewer.
        description = None if args.no_pr_context else f"# {t.get('title', '')}\n\n{t.get('body') or ''}"
        change = Change(key, split_diff(diff))
        result = await run_review(repo, change, cfg, finder, None, None, archive_root=CACHE / "runs",
                                  description=description)
        jb = llm.Budget(1.0)
        pairs = await judge(golden, result.findings, judge_model, jb)
        spent += result.cost_usd + jb.spent
        hit = {g for g, _ in pairs}
        serious = [f"g{i}" for i, x in enumerate(golden) if str(x.get("severity", "")).lower() in SERIOUS]
        row = {"pr": key, "golden": len(golden), "caught": len(hit), "serious": len(serious),
               "serious_caught": len(hit & set(serious)), "findings": len(result.findings),
               "matched": len(pairs), "incomplete": bool(result.incomplete),
               "cost": round(result.cost_usd + jb.spent, 4)}
        rows.append(row)
        print(f"{key[:44]:<44} caught {row['caught']}/{row['golden']} · serious {row['serious_caught']}/"
              f"{row['serious']} · findings {row['findings']} ({row['matched']} matched) · ${row['cost']:.3f}"
              + (" · incomplete" if row["incomplete"] else ""))
    tot = {k: sum(r[k] for r in rows) for k in ("golden", "caught", "serious", "serious_caught", "findings", "matched")}
    summary = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"), "label": args.label, "model": finder,
        "prs": len(rows), "recall": round(tot["caught"] / max(tot["golden"], 1), 3),
        "serious_recall": round(tot["serious_caught"] / max(tot["serious"], 1), 3),
        "precision": round(tot["matched"] / max(tot["findings"], 1), 3), "cost_usd": round(spent, 4),
        "rows": rows,
    }
    print(f"\n{args.label}: recall {summary['recall']:.0%} · serious recall {summary['serious_recall']:.0%} · "
          f"precision {summary['precision']:.0%} (lower bound) · ${spent:.2f} over {len(rows)} PRs")
    with RESULTS.open("a") as f:
        f.write(json.dumps(summary) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--judge", default="claude-haiku-4-5")
    ap.add_argument("--max-lines", type=int, default=500)
    ap.add_argument("--per-review", type=float, default=0.5)
    ap.add_argument("--cap", type=float, default=5.0, help="stop before total spend could cross this")
    ap.add_argument("--no-pr-context", action="store_true", help="don't give the reviewer the PR description")
    ap.add_argument("--limit", type=int, help="only the first N PRs (keeps runs comparable)")
    ap.add_argument("--min-severity", default="minor", choices=[s.value for s in Severity])
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
