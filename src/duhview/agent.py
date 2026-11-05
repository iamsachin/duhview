"""A plain tool-calling loop: ask, run requested tools, feed results back, until a final answer."""

import re
from typing import Any

from pydantic import BaseModel, ValidationError

from duhview import model as llm
from duhview.tools import RepoTools

EXTRA_TURNS = 5  # turns allowed after the tool budget is spent, to produce the answer
RESERVE_TURNS = 3  # wind down when spend + this many turns at the last turn's cost would exceed the share


class AgentError(Exception):
    pass


async def run_agent(model: str, messages: list[dict[str, Any]], tools: RepoTools | None,
                    budget: llm.Budget, max_tool_calls: int, max_usd: float | None = None) -> str:
    """`max_usd` is this agent's share of the budget. Near it, tools stop answering and the agent is
    told to give its final answer, so it reports what it has found instead of being cut off."""
    calls, spent, last_cost = 0, 0.0, 0.0
    for _ in range(max_tool_calls + EXTRA_TURNS):
        resp = await llm.complete(model, messages, tools.schemas if tools else None, budget)
        last_cost = llm.cost_of(resp) or 0.0
        spent += last_cost
        wind_down = max_usd is not None and spent + RESERVE_TURNS * last_cost >= max_usd
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        entry: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if tool_calls:
            entry["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ]
        messages.append(entry)
        if not tool_calls:
            return msg.content or ""
        for tc in tool_calls:
            calls += 1
            if tools is None or calls > max_tool_calls or wind_down:
                result = "error: tool call limit reached. Give your final JSON answer now."
            else:
                result = await tools.call(tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    raise AgentError("agent did not produce a final answer")


def parse_json[T: BaseModel](text: str, cls: type[T]) -> T:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    return cls.model_validate_json(text[start : end + 1])


async def ask_json[T: BaseModel](model: str, system: str, user: str, cls: type[T], tools: RepoTools | None,
                                 budget: llm.Budget, max_tool_calls: int, log: list[Any],
                                 max_usd: float | None = None) -> T:
    """Run the agent and parse its answer into `cls`, with one repair attempt."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    log.append(messages)  # appended first so failed runs are archived too
    text = await run_agent(model, messages, tools, budget, max_tool_calls, max_usd)
    try:
        return parse_json(text, cls)
    except (ValidationError, ValueError) as e:
        messages.append({"role": "user", "content": f"Your answer did not match the required JSON schema: {e}\n"
                                                    "Reply with only the corrected JSON object."})
        return parse_json(await run_agent(model, messages, tools, budget, max_tool_calls, max_usd), cls)
