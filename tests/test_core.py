import asyncio
import json
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


def test_litellm_tool_calling_path_imports() -> None:
    """LiteLLM imports `mcp` on every tool-calling request without declaring it, and an old `mcp`
    (seen when semgrep was installed alongside) breaks the import. Mocked: no network, no key."""
    import litellm

    tools = [{"type": "function", "function": {"name": "x", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    r = asyncio.run(litellm.acompletion(model="openai/gpt-5.5", messages=[{"role": "user", "content": "hi"}],
                                        tools=tools, mock_response="ok", api_key="x"))
    assert r.choices[0].message.content == "ok"


# --- several finder models ---------------------------------------------------------------------


def test_resolve_a_list_of_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert model.resolve_model("claude-sonnet-5-5, gpt-5.5") == "anthropic/claude-sonnet-5-5,openai/gpt-5.5"
    with pytest.raises(model.ModelError):
        model.resolve_model("claude-sonnet-5-5,gemini-2.5-pro")


def test_two_finder_models_are_merged_with_agreement(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from duhview.config import Config
    from duhview.review import run_review

    def draft(line: int, title: str, severity: str = "major") -> dict[str, Any]:
        return {"file": "src/app.py", "start_line": line, "end_line": line, "severity": severity,
                "category": "bug", "title": title, "explanation": "e"}

    answers = {"A": [draft(9, "only A sees this"), draft(1, "both see this")],
               "B": [draft(1, "both, worded differently")]}

    async def fake_complete(m: str, messages: list[dict[str, Any]], tools: Any, budget: Any) -> Any:
        return _resp(content=json.dumps({"findings": answers[m]}))

    monkeypatch.setattr(model, "complete", fake_complete)
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    cfg = Config(scanners=[])
    result = asyncio.run(run_review(repo, get_change(repo, Target(), cfg), cfg, "A,B", None, None))
    assert [(f.title, f.models) for f in result.findings] == [
        ("both see this", ["A", "B"]),  # agreed findings come first within a severity
        ("only A sees this", ["A"]),
    ]
    assert result.model == "A,B"


def test_cross_model_duplicates_merge_within_a_wider_window() -> None:
    """Seen live on a real repo: two models flagged the same bug at lines 376 and 381."""
    from duhview.review import _dedupe

    def f(line: int, title: str, m: str) -> Finding:
        return Finding.from_draft(FindingDraft(file="Page.vue", start_line=line, end_line=line, severity="major",
                                               category="bug", title=title, explanation="e")).model_copy(
            update={"models": [m]})

    merged = _dedupe([f(381, "chips can't jump to batched sub-agents", "sonnet"),
                      f(376, "chip-strip jump fails for collapsed sub-agents", "gpt")])
    assert [x.models for x in merged] == [["sonnet", "gpt"]]
    # One model's own findings 5 lines apart stay separate.
    assert len(_dedupe([f(381, "a", "sonnet"), f(376, "b", "sonnet")])) == 2


# --- the hard cost cap -----------------------------------------------------------------------


def test_parallel_calls_never_jointly_cross_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    peak: list[float] = []
    budget = model.Budget(1.0)

    async def fake_acompletion(**kwargs: Any) -> Any:
        assert kwargs["max_tokens"] == model.MAX_OUTPUT_TOKENS  # each call's output is bounded
        await asyncio.sleep(0.01)
        return "response"

    def fake_cost(response: Any) -> float:
        return 0.3

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(model, "worst_case_cost", lambda m, msgs, tools: 0.4)
    monkeypatch.setattr(model, "cost_of", fake_cost)

    async def one() -> str:
        try:
            await model.complete("m", [], None, budget)
            peak.append(budget.spent + budget.reserved)
            return "ok"
        except model.BudgetExceeded:
            return "refused"

    async def main() -> list[str]:
        return list(await asyncio.gather(*(one() for _ in range(4))))

    results = asyncio.run(main())
    assert sorted(results) == ["ok", "ok", "ok", "refused"]
    assert budget.spent == pytest.approx(0.9) and budget.reserved == 0
    assert max(peak) <= 1.0  # spent + in flight never exceeded the cap


def test_worst_case_cost_needs_a_known_price() -> None:
    msgs = [{"role": "user", "content": "hello " * 50}]
    import litellm

    est = model.worst_case_cost("anthropic/claude-sonnet-5-5", msgs, None)
    out_price = litellm.model_cost["claude-sonnet-5-5"]["output_cost_per_token"]
    assert est is not None and est > model.MAX_OUTPUT_TOKENS * out_price  # max output plus some input
    assert model.worst_case_cost("openai/not-a-real-model", msgs, None) is None
