"""Model selection, LLM calls and cost accounting."""

import asyncio
import json
import os
from typing import Any

import litellm

API_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
REQUEST_TIMEOUT_S = 180

litellm.suppress_debug_info = True


class ModelError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


def resolve_model(name: str) -> str:
    """Infer the provider from a model name; return the provider-qualified name. A comma-separated
    list (several finder models) resolves each and stays comma-separated."""
    if "," in name:
        return ",".join(resolve_model(n.strip()) for n in name.split(",") if n.strip())
    try:
        model, provider, _, _ = litellm.get_llm_provider(name)
    except Exception as e:
        raise ModelError(f"unknown model '{name}'") from e
    if provider not in API_KEYS:
        raise ModelError(f"'{name}' is a {provider} model; duhview supports Anthropic and OpenAI models")
    if not os.environ.get(API_KEYS[provider]):
        raise ModelError(f"{API_KEYS[provider]} is not set (needed for {name})")
    return f"{provider}/{model}"


def cost_of(response: Any) -> float | None:
    """USD cost of one response, or None when LiteLLM has no price for the model."""
    try:
        return float(litellm.completion_cost(completion_response=response))
    except Exception:  # noqa: BLE001 - LiteLLM raises various errors for unpriced models
        return None


MAX_OUTPUT_TOKENS = 12_000  # per call; bounds the worst-case cost of each call
INPUT_ESTIMATE_MARGIN = 1.1  # token counts are estimates for some providers


def worst_case_cost(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> float | None:
    """Most a call can cost: its input plus the maximum output. None when the model has no known price."""
    price = litellm.model_cost.get(model) or litellm.model_cost.get(model.split("/", 1)[-1])
    if not price or "input_cost_per_token" not in price or "output_cost_per_token" not in price:
        return None
    try:
        tokens = litellm.token_counter(model=model, messages=messages)
    except Exception:  # noqa: BLE001 - fall back to a rough count rather than skip the cap
        tokens = sum(len(str(m.get("content") or "")) for m in messages) // 3
    tokens += len(json.dumps(tools)) // 3 if tools else 0
    return (tokens * INPUT_ESTIMATE_MARGIN * price["input_cost_per_token"]
            + MAX_OUTPUT_TOKENS * price["output_cost_per_token"])


class Budget:
    """A hard cap: each call reserves its worst-case cost before it is sent, so calls running in
    parallel can never jointly cross the cap. A call that doesn't fit waits for in-flight calls
    to finish; only when nothing is in flight and it still doesn't fit does the review stop."""

    def __init__(self, max_usd: float):
        self.max_usd = max_usd
        self.spent = 0.0
        self.reserved = 0.0
        self.known = True  # False once any call could not be priced
        self._cond: asyncio.Condition | None = None  # created inside the running event loop

    def charge(self, response: Any) -> None:
        cost = cost_of(response)
        if cost is None:
            self.known = False
        else:
            self.spent += cost

    def check(self) -> None:
        if self.spent >= self.max_usd:
            raise BudgetExceeded(f"budget of ${self.max_usd:.2f} used up")

    async def reserve(self, amount: float) -> None:
        self._cond = self._cond or asyncio.Condition()
        async with self._cond:
            while self.spent + self.reserved + amount > self.max_usd:
                if self.reserved <= 0:
                    raise BudgetExceeded(f"budget of ${self.max_usd:.2f} used up "
                                         f"(spent ${self.spent:.2f}; the next call could cost ${amount:.2f})")
                await self._cond.wait()
            self.reserved += amount

    async def settle(self, amount: float, response: Any | None) -> None:
        """Release a reservation and charge what the call actually cost."""
        self._cond = self._cond or asyncio.Condition()
        async with self._cond:
            self.reserved = max(0.0, self.reserved - amount)
            if response is not None:
                self.charge(response)
            self._cond.notify_all()


async def complete(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                   budget: Budget) -> Any:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "timeout": REQUEST_TIMEOUT_S,
                              "max_tokens": MAX_OUTPUT_TOKENS}
    if tools:
        kwargs["tools"] = tools
    estimate = worst_case_cost(model, messages, tools)
    if estimate is None:  # unpriced model: can't bound the next call, so stop once the cap is reached
        budget.check()
        response = await litellm.acompletion(**kwargs)
        budget.charge(response)
        return response
    await budget.reserve(estimate)
    response = None
    try:
        response = await litellm.acompletion(**kwargs)
    finally:
        await budget.settle(estimate, response)
    return response
