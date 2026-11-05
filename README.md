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
duhview review --sarif out.sarif     # also write SARIF (GitHub code scanning, CI tools)
duhview review --offline             # skip scanners that need the network
duhview review --pr 42 --post        # review a GitHub PR and post the result on it
```

Default model: `claude-sonnet-5-5`. Only Anthropic and OpenAI models are supported.

The verify pass is off by default. With a strong model it adds cost without improving precision;
with a cheaper finder model it removes most false positives. Turn it on with `--verifier-model`.

Exit codes: `0` clean · `1` findings at or above `block_on_severity` · `2` review incomplete · `3` error.

Each run is archived (prompts, model output, rejected findings, and the report as Markdown, JSON
and SARIF) in `.duhview/runs/`, which ignores itself in git.

## How a review runs

1. **Collect** the diff. Lockfiles, minified files and source maps are skipped by default.
2. **Scan** with [gitleaks](https://github.com/gitleaks/gitleaks) and [semgrep](https://semgrep.dev)
   if they are installed. Only findings on lines the change adds are kept. semgrep uses a local
   `.semgrep.yml` if present, otherwise the `p/default` registry ruleset (needs the network).
3. **Find**: changed files are packed into review units; one agent reviews each unit in parallel,
   with scanner results as leads. Each agent gets a share of the budget and is told to wrap up
   and report what it has found as it nears its share.
4. **Verify** (optional): each agent finding is re-checked in a fresh context.


## Pull requests and GitHub Actions

`duhview review --pr <url or number>` checks the pull request out into a temporary git worktree
(your own checkout is not touched) and reviews it against its base. `--post` publishes one
review on the PR: findings on lines shown in the diff become inline comments, with a one-click
suggestion when the fix is a direct replacement; the rest go in the summary. The review is
always a plain comment; it never blocks merging by itself. The token comes from `GITHUB_TOKEN`,
`GH_TOKEN` or `gh auth`.

In PR mode, `.duhview/config.yaml`, `.duhview/rules.md` and `.duhview/fp-filter.md` are read
from the **base branch**, so a pull request cannot weaken its own review. Changes it makes to
those files are reviewed like any other change.

To review every pull request automatically, add your model API key as a repository secret
(`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) and this workflow:

```yaml
name: duhview
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: iamsachin/duhview@main
        # with:
        #   model: gpt-5.5     # optional; defaults to the base branch's config
        #   scanners: "false" # skip installing gitleaks and semgrep (on by default)
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

The Action installs [gitleaks](https://github.com/gitleaks/gitleaks) (pinned version, checksum
verified) and [semgrep](https://semgrep.dev) before reviewing; this adds about a minute.

The job fails when a finding reaches `block_on_severity` (or the review is incomplete), so you
can make it a required check with branch protection. Pull requests from forks are skipped:
GitHub does not give them secrets, and running untrusted code with secrets is unsafe.

## Configuration

Optional `.duhview/config.yaml`:

```yaml
model:
  name: claude-sonnet-5-5
  verifier: null            # set a model to enable the verify pass
review:
  block_on_severity: major
  min_severity: minor
  exclude: ["dist/*", "*.lock"]   # replaces the default lockfile/minified excludes
rules:
  - text: "Handlers must not run raw SQL; use the repository layer."
    paths: ["app/handlers/*"]
budget:
  max_usd_per_review: 2.00
  max_tool_calls_per_agent: 40
  agent_timeout_s: 300
scanners: [gitleaks, semgrep]   # omit to run whichever are installed; listed ones are required
offline: false
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
