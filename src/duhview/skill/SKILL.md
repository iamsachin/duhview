---
name: duhview
description: Review your own code changes with duhview before handing work back, committing or pushing. Use after any non-trivial code change, or when the user asks you to review changes.
---

# Review changes with duhview

duhview is an agentic code reviewer. It reads the diff, explores the repository with read-only
tools, and reports findings with file, line, severity and a suggested fix.

## When to use

- After you finish a non-trivial change, before telling the user you are done.
- Before committing or pushing, or when the user asks for a review.

## How

1. Run the review and capture JSON:
   - uncommitted changes (default): `duhview review --json`
   - staged only: `duhview review --staged --json`
   - a branch: `duhview review --branch main --json`
2. Exit codes: `0` clean, `1` findings at or above the configured severity, `2` the review was
   incomplete (say so to the user; do not treat it as clean), `3` error (e.g. missing API key).
3. In the JSON, `findings` is a list with `file`, `start_line`, `end_line`, `severity`,
   `category`, `title`, `explanation` and optionally `suggestion` (replacement code for those
   lines). `incomplete` lists parts that were not reviewed.
4. Show the user one line per finding (severity, file:line, title), then ask:
   **"Fix all, or tell me which?"** Fix only the findings they choose. Re-run the review after.

## Rules

- If you believe a finding is wrong, say why instead of changing code to silence it.
- Never edit `.duhview/` config, rules or the false-positive filter to make a review pass.
- Prefer a different model from the one that wrote the code, e.g. `--model gpt-5.5` or
  `--model claude-sonnet-5-5`: a reviewer is less likely to repeat the author's blind spots.
- Each review calls a paid model API (typically a few cents). Don't loop it without changes.
