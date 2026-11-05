"""Model selection, LLM calls and cost accounting."""

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
    """Infer the provider from a model name; return the provider-qualified name."""
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


class Budget:
    def __init__(self, max_usd: float):
        self.max_usd = max_usd
        self.spent = 0.0
        self.known = True  # False once any call could not be priced

    def charge(self, response: Any) -> None:
        cost = cost_of(response)
        if cost is None:
            self.known = False
        else:
            self.spent += cost

    def check(self) -> None:
        if self.spent >= self.max_usd:
            raise BudgetExceeded(f"budget of ${self.max_usd:.2f} used up")


async def complete(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                   budget: Budget) -> Any:
    budget.check()
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "timeout": REQUEST_TIMEOUT_S}
    if tools:
        kwargs["tools"] = tools
    response = await litellm.acompletion(**kwargs)
    budget.charge(response)
    return response
