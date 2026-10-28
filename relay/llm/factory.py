"""Build the model client for one run, and decide which model and tools a run uses.

WHAT IT IS  The switchboard. A run is either `demo` (scripted fake model + fake
            tools: $0, what public visitors get) or `live` (the real model +
            real tools: what a live tenant's runs get). A run with no mode (the
            harnesses, old runs) follows LLM_PROVIDER / TOOLS_PROVIDER.
GOES IN     resolve_mode(mode) -> (llm_provider, tools_provider).
            make_llm(provider) / planner_model(provider).
COMES OUT   An LLMClient and the model id to ask it for.
TOUCHES     Nothing at call time. The real client is built once per process
            (the SDK keeps a connection pool) on first use.
FAILS WHEN  A provider with no client, or a model with no price -> RuntimeError
            / UnknownModelPrice. The loop turns either into an `error` step
            (`model_misconfigured`) before any money is spent; preflight()
            runs the same checks when a worker starts.
"""

import os
from typing import Literal

from relay.agent.fake_script import enrich_script
from relay.config import settings
from relay.llm.client import LLMClient
from relay.llm.fake import FakeLLMClient
from relay.llm.pricing import assert_priced

RunMode = Literal["demo", "live"]
_MODES: dict[str, tuple[str, str]] = {"demo": ("fake", "fake"), "live": ("anthropic", "real")}

_anthropic_client: LLMClient | None = None


def resolve_mode(mode: str | None) -> tuple[str, str]:
    """(llm_provider, tools_provider) for a run. Unknown mode -> RuntimeError."""
    if mode is None:
        return settings.llm_provider, settings.tools_provider
    if mode not in _MODES:
        raise RuntimeError(f"unknown run mode {mode!r}")
    return _MODES[mode]


def planner_model(provider: str | None = None) -> str:
    provider = provider or settings.llm_provider
    if provider == "fake":
        return settings.model_planner if settings.llm_provider == "fake" else "fake-planner"
    return settings.anthropic_model


def make_llm(provider: str | None = None) -> LLMClient:
    """Return the client for one run, chosen by its provider."""
    global _anthropic_client
    provider = provider or settings.llm_provider
    if provider == "fake":
        return FakeLLMClient(
            enrich_script(settings.fake_broken_final),
            delay_seconds=settings.fake_delay_seconds,
            failure_rate=settings.fake_model_failure_rate,
        )
    if provider == "anthropic":
        assert_priced(settings.anthropic_model)  # never spend on a model the budget can't price
        if _anthropic_client is None:
            from relay.llm.anthropic_client import AnthropicLLMClient

            _anthropic_client = AnthropicLLMClient()
        return _anthropic_client
    raise RuntimeError(f"LLM provider {provider!r} has no client")


def preflight() -> list[str]:
    """Worker start-up check, no network: what can this worker run? Returns warnings."""
    warnings: list[str] = []
    assert_priced(planner_model())
    if settings.llm_provider == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        assert_priced(settings.anthropic_model)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY not set: live runs will fail with model_fatal")
    if not settings.tavily_api_key:
        warnings.append("TAVILY_API_KEY not set: live runs' web_search will be unavailable")
    return warnings
