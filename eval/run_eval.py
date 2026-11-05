"""Run every planted-bug case without and with the verify pass; append metrics to eval/results.jsonl.

    .venv/bin/python eval/run_eval.py --model gpt-5.5 [--repeat 2] [--case py_auth,ts_logic]
"""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from duhview.bench import load_cases, run_bench, table, totals
from duhview.model import resolve_model

RESULTS = Path(__file__).parent / "results.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--verifier-model", help="verifier for the with-verifier pass (default: --model)")
    ap.add_argument("--repeat", type=int, default=1, help="runs per mode; LLM output varies run to run")
    ap.add_argument("--case", help="comma-separated case names (default: all)")
    args = ap.parse_args()

    finder = resolve_model(args.model)
    verifier = resolve_model(args.verifier_model or args.model)
    cases = load_cases(args.case.split(",") if args.case else None)

    for run in range(args.repeat):
        for mode in (None, verifier):
            scores = asyncio.run(run_bench(cases, finder, mode))
            label = f"with verifier {mode}" if mode else "without verifier"
            print(f"\n== run {run + 1}/{args.repeat}, {label} ==\n{table(scores)}")
            record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "model": finder,
                      "verifier": mode, **totals(scores),
                      "cases": [asdict(s) for s in scores]}
            with RESULTS.open("a") as f:
                f.write(json.dumps(record) + "\n")
    print(f"\nappended to {RESULTS}")


if __name__ == "__main__":
    main()
