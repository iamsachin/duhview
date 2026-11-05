# duhview

Agentic code review for your changes, before you push. duhview reads your diff and explores the
repository with read-only tools so every finding points at real code. An optional verify pass
re-checks each finding in a fresh context.

## Install

```sh
pipx install .        # or: uv tool install .
```

Set the API key for the model you use: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

## Usage

```sh
duhview review                       # uncommitted changes (incl. new files)
duhview review --staged              # staged changes only
duhview review --branch main         # main...HEAD
duhview review --commit abc123       # one commit
duhview review --model gpt-5.5       # provider is inferred from the model name
duhview review --model gpt-5.4-mini --verifier-model gpt-5.5   # cheap finder + verify pass
duhview review --spec docs/feature.md   # also check against requirements
duhview review --json                # machine-readable output
```

Default model: `claude-sonnet-5-5`. Only Anthropic and OpenAI models are supported.

The verify pass is off by default. With a strong model it adds cost without improving precision;
with a cheaper finder model it removes most false positives. Turn it on with `--verifier-model`.

Exit codes: `0` clean · `1` findings at or above `block_on_severity` · `2` review incomplete · `3` error.

Each run is archived (prompts, model output, rejected findings, report) in `.duhview/runs/`,
which ignores itself in git.

## Configuration

Optional `.duhview/config.yaml`:

```yaml
model:
  name: claude-sonnet-5-5
  verifier: null            # set a model to enable the verify pass
review:
  block_on_severity: major
  min_severity: minor
  exclude: ["dist/*", "*.lock"]
rules:
  - text: "Handlers must not run raw SQL; use the repository layer."
    paths: ["app/handlers/*"]
budget:
  max_usd_per_review: 1.00
  max_tool_calls_per_agent: 40
  agent_timeout_s: 300
```

duhview also reads `AGENTS.md`, `CLAUDE.md` and `.duhview/rules.md` as project context, and
`.duhview/fp-filter.md` as a list of things the team does not want reported.

## Demo and evaluation

duhview ships with small repos that contain planted bugs, plus clean changes where any finding
is a false positive.

```sh
duhview demo                       # one case
duhview demo --case all --model gpt-5.5
python eval/run_eval.py --model gpt-5.5 --repeat 2   # with and without the verifier
```

`eval/run_eval.py` appends recall, precision, false positives and cost to `eval/results.jsonl`.

## Safety

- The agent's tools are read-only and run in-process, never through a shell.
- It can only see files git would commit: paths outside the repo, symlinks that escape it, and
  gitignored files (such as `.env`) are refused.
- Diff and file contents are passed as untrusted data. With a verifier model set, every finding
  is re-checked in a fresh context.
- Each review has a cost cap, a tool-call cap and timeouts.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/mypy src
```

## License

MIT
