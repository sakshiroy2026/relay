"""Build the LLM client chosen by settings.llm_provider."""

from relay.agent.fake_script import enrich_script
from relay.config import settings
from relay.llm.client import LLMClient
from relay.llm.fake import FakeLLMClient


def make_llm() -> LLMClient:
    """Return a fresh client for one run, chosen by config."""
    if settings.llm_provider == "fake":
        return FakeLLMClient(
            enrich_script(settings.fake_broken_final),
            delay_seconds=settings.fake_delay_seconds,
            failure_rate=settings.fake_model_failure_rate,
        )
    raise RuntimeError(
        f"LLM_PROVIDER={settings.llm_provider!r} has no client yet "
        "(relay/llm/anthropic.py is built at demo time)"
    )
