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
            escalation_model(provider) / fallback_model(provider) (P5): each
            provider has its own pair, and a model from another provider's
            family is dropped ("" = off), so a Gemini run can never be handed
            a Claude id it cannot call.
COMES OUT   An LLMClient and the model id(s) to ask it for.
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
    """Can this provider run P5's rung-3 escalation / outage fallback at all?

    Only a real provider: the fake is a script, and "a stronger fake" would
    prove nothing. Which model it may use is escalation_model() / fallback_model().
    """
    return (provider or settings.llm_provider) in MODEL_FAMILY


# A run's client can only call its own provider's models: a Claude id on a
# Gemini run would fail the moment the path was taken, and vice versa.
MODEL_FAMILY = {"anthropic": "claude-", "gemini": "gemini-"}


def _configured(provider: str, kind: Literal["escalation", "fallback"]) -> str:
    """The setting for this provider's escalation / fallback model, as written."""
    if provider == "gemini":
        return (
            settings.gemini_escalation_model
            if kind == "escalation"
            else settings.gemini_fallback_model
        )
    return settings.escalation_model if kind == "escalation" else settings.fallback_model


def _usable(provider: str, kind: Literal["escalation", "fallback"]) -> str:
    """The configured model if this provider's client can call it, else "" (= off)."""
    candidate = _configured(provider, kind)
    prefix = MODEL_FAMILY.get(provider)
    return candidate if prefix and candidate.startswith(prefix) else ""


def escalation_model(provider: str | None = None) -> str:
    """Rung 3's model for this provider's runs; "" when it has none."""
    return _usable(provider or settings.llm_provider, "escalation")


def fallback_model(provider: str | None = None) -> str:
    """The outage fallback for this provider's runs; "" when it has none."""
    return _usable(provider or settings.llm_provider, "fallback")


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
    providers = ["gemini"]
    if settings.llm_provider == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        assert_priced(settings.anthropic_model)
        providers.append("anthropic")
    # P5: an escalation / fallback model is priced before any run needs it
    # (can_afford() estimates it mid-run); a cross-family id is named, not used.
    kinds: tuple[Literal["escalation", "fallback"], ...] = ("escalation", "fallback")
    for provider in providers:
        for kind in kinds:
            if usable := _usable(provider, kind):
                assert_priced(usable)
            elif configured := _configured(provider, kind):
                warnings.append(
                    f"{kind} model {configured!r} is not a {provider} model: "
                    f"{provider} runs will not use it"
                )
    if not os.environ.get("GEMINI_API_KEY"):
        warnings.append(
            "GEMINI_API_KEY not set: live and public real-AI runs will fail "
            "with model_misconfigured"
        )
    if not settings.tavily_api_key:
        warnings.append("TAVILY_API_KEY not set: live runs' web_search will be unavailable")
    return warnings
