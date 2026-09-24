"""Build the LLM client chosen by settings.llm_provider."""

from relay.agent.fake_script import ENRICH_SCRIPT
from relay.config import settings
from relay.llm.client import LLMClient
from relay.llm.fake import FakeLLMClient


def make_llm() -> LLMClient:
    """Return a fresh client for one run, chosen by config."""
    if settings.llm_provider == "fake":
        return FakeLLMClient(ENRICH_SCRIPT)
    raise RuntimeError(
        f"LLM_PROVIDER={settings.llm_provider!r} has no client yet "
        "(relay/llm/anthropic.py is built at demo time)"
    )
