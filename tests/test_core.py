import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from duhview import agent, model
from duhview.collect import Target, get_change
from duhview.config import Config, ReviewCfg
from duhview.report import exit_code
from duhview.tools import RepoTools
from duhview.types import FinderOutput, Finding, FindingDraft, ReviewResult, Severity

# --- collect ----------------------------------------------------------------------------------


def test_uncommitted_change_includes_edits_and_new_files(repo: Path) -> None:
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "src" / "new.py").write_text("print('hi')\n")
    change = get_change(repo, Target(), Config())
    assert set(change.files) == {"src/app.py", "src/new.py"}
    assert "-    return a + b" in change.diff and "+print('hi')" in change.diff


def test_exclude_globs_drop_files(repo: Path) -> None:
    (repo / "README.md").write_text("changed\n")
    (repo / "src" / "app.py").write_text("x\n")
    change = get_change(repo, Target(), Config(review=ReviewCfg(exclude=["*.md"])))
    assert list(change.files) == ["src/app.py"]


def test_staged_only(repo: Path) -> None:
    (repo / "README.md").write_text("changed\n")
    assert get_change(repo, Target(staged=True), Config()).files == {}


# --- model selection --------------------------------------------------------------------------


def test_resolve_model_infers_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert model.resolve_model("claude-sonnet-5-5") == "anthropic/claude-sonnet-5-5"
    assert model.resolve_model("gpt-5.5") == "openai/gpt-5.5"


def test_resolve_model_rejects_other_providers_and_missing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(model.ModelError, match="supports Anthropic and OpenAI"):
        model.resolve_model("gemini-2.5-pro")
    with pytest.raises(model.ModelError, match="ANTHROPIC_API_KEY"):
        model.resolve_model("claude-sonnet-5-5")
    with pytest.raises(model.ModelError, match="unknown model"):
        model.resolve_model("not-a-model-xyz")


# --- agent loop -------------------------------------------------------------------------------


def _resp(content: str | None = None, tool: tuple[str, str] | None = None) -> Any:
    calls = [SimpleNamespace(id="c1", function=SimpleNamespace(name=tool[0], arguments=tool[1]))] if tool else None
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


def test_agent_runs_tools_and_parses_answer(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = ('```json\n{"findings": [{"file": "src/app.py", "start_line": 2, "end_line": 2, "severity": "major",'
              ' "category": "bug", "title": "t", "explanation": "e"}]}\n```')
    script = [_resp(tool=("read_file", '{"path": "src/app.py"}')), _resp(content=answer)]
    seen: list[list[dict[str, Any]]] = []

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        seen.append(list(messages))
        return script.pop(0)

    monkeypatch.setattr(model, "complete", fake_complete)
    log: list[Any] = []
    out = asyncio.run(agent.ask_json("m", "sys", "user", FinderOutput, RepoTools(repo), model.Budget(1), 5, log))
    assert out.findings[0].file == "src/app.py"
    assert seen[1][-1] == {"role": "tool", "tool_call_id": "c1", "content": "1\tdef add(a, b):\n2\t    return a + b"}
    assert log and log[0][-1]["role"] == "assistant"


def test_agent_repairs_bad_json_once(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [_resp(content="not json"), _resp(content='{"findings": []}')]

    async def fake_complete(*_: Any) -> Any:
        return script.pop(0)

    monkeypatch.setattr(model, "complete", fake_complete)
    out = asyncio.run(agent.ask_json("m", "s", "u", FinderOutput, None, model.Budget(1), 0, []))
    assert out.findings == []


def test_agent_stops_runaway_tool_use(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def always_tool(*_: Any) -> Any:
        return _resp(tool=("list_dir", "{}"))

    monkeypatch.setattr(model, "complete", always_tool)
    with pytest.raises(agent.AgentError):
        asyncio.run(agent.run_agent("m", [], RepoTools(repo), model.Budget(1), 3))


def test_budget_stops_calls() -> None:
    b = model.Budget(0.5)
    b.spent = 0.5
    with pytest.raises(model.BudgetExceeded):
        b.check()


# --- exit codes -------------------------------------------------------------------------------


def _result(sev: Severity | None, incomplete: list[str] | None = None) -> ReviewResult:
    findings = [] if sev is None else [Finding.from_draft(FindingDraft(
        file="a", start_line=1, end_line=1, severity=sev, category="bug", title="t", explanation="e"))]
    return ReviewResult(target="t", model="m", verifier_model="m", findings=findings,
                        incomplete=incomplete or [], cost_usd=0, cost_known=True)


@pytest.mark.parametrize(("sev", "incomplete", "code"), [
    (None, None, 0), (Severity.minor, None, 0), (Severity.major, None, 1),
    (Severity.critical, None, 1), (None, ["x"], 2), (Severity.critical, ["x"], 2),
])
def test_exit_codes(sev: Severity | None, incomplete: list[str] | None, code: int) -> None:
    assert exit_code(_result(sev, incomplete), Severity.major) == code
