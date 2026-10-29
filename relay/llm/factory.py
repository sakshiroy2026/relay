"""Build the model client for one run, and decide which model and tools a run uses.

WHAT IT IS  The switchboard. A run is `demo` (scripted fake model + fake tools:
            $0, the instant demo every public visitor gets by default),
            `public_real` (P7, Option A: the real free-tier model + the FAKE
            tools, so a stranger's run can never make the server fetch a URL of
            their choosing), or `live` (the real model + real tools: what a live
            tenant's runs get). A run with no mode (the harnesses, old runs)
            follows LLM_PROVIDER / TOOLS_PROVIDER.
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

RunMode = Literal["demo", "public_real", "live"]
_MODES: dict[str, tuple[str, str]] = {
    "demo": ("fake", "fake"),
    "public_real": ("gemini", "fake"),  # real model, pretend internet (P7)
    "live": ("gemini", "real"),
}
# Which providers charge real money per call. The free tier bills $0, but a
# Gemini run is still a real model call that the ledger prices and caps.
REAL_PROVIDERS = ("anthropic", "gemini")

_clients: dict[str, LLMClient] = {}


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
    if provider == "gemini":
        return settings.gemini_model
    return settings.anthropic_model


def escalation_allowed(provider: str | None = None) -> bool:
    """Can this provider run P5's rung-3 escalation / outage fallback?

    Only Anthropic. ESCALATION_MODEL and FALLBACK_MODEL name Claude models, and
    a Gemini client cannot call them: offering them on a Gemini run would put a
    model on a path that would fail the moment it was taken. Gemini runs keep
    rungs 0-2 of the repair ladder, which need no second provider.
    """
    return (provider or settings.llm_provider) == "anthropic"


def make_llm(provider: str | None = None) -> LLMClient:
    """Return the client for one run, chosen by its provider."""
    provider = provider or settings.llm_provider
    if provider == "fake":
        return FakeLLMClient(
            enrich_script(settings.fake_broken_final),
            delay_seconds=settings.fake_delay_seconds,
            failure_rate=settings.fake_model_failure_rate,
        )
    if provider in REAL_PROVIDERS:
        assert_priced(planner_model(provider))  # never spend on a model the budget can't price
        if provider not in _clients:
            if provider == "anthropic":
                from relay.llm.anthropic_client import AnthropicLLMClient

                _clients[provider] = AnthropicLLMClient()
            else:
                from relay.llm.gemini_client import GeminiLLMClient

                _clients[provider] = GeminiLLMClient()
        return _clients[provider]
    raise RuntimeError(f"LLM provider {provider!r} has no client")


def preflight() -> list[str]:
    """Worker start-up check, no network: what can this worker run? Returns warnings."""
    warnings: list[str] = []
    assert_priced(planner_model())
    # Every model this worker could be asked for must have a price BEFORE a
    # call is made: `live` and `public_real` runs arrive from the API, not from
    # this worker's own settings.
    assert_priced(settings.gemini_model)
    if settings.llm_provider == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        assert_priced(settings.anthropic_model)
    if not os.environ.get("GEMINI_API_KEY"):
        warnings.append(
            "GEMINI_API_KEY not set: live and public real-AI runs will fail "
            "with model_misconfigured"
        )
    if not settings.tavily_api_key:
        warnings.append("TAVILY_API_KEY not set: live runs' web_search will be unavailable")
    return warnings
