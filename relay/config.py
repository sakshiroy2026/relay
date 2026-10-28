from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    dev_tenant_id: str
    lease_seconds: int = 90
    relay_api_key: str | None = None  # safety slice: unset -> /v1 refuses every request
    # ── LLM (Day 4) ─────────────────────────────────────────────
    llm_provider: Literal["fake", "anthropic"] = "fake"
    model_planner: str = "fake-planner"
    model_cheap: str = "fake-cheap"
    tools_provider: Literal["fake", "real"] = "fake"
    # ── Real tools (P2): Tavily search + pinned-IP page fetcher ─
    tavily_api_key: str | None = None  # required when tools_provider = "real"; never logged
    page_text_max_chars: int = (
        8000  # ~2k tokens of page text per fetch: the model's main input cost
    )
    fake_delay_seconds: float = 0.0  # Day 6: slow the fake model down so a run can be killed
    # ── Real model (P1) ─────────────────────────────────────────
    # The key itself is read by the SDK from ANTHROPIC_API_KEY; it never passes
    # through Settings, so it can't end up in a log of the settings object.
    anthropic_model: str = "claude-haiku-5-5"  # the cheapest current model: the dev default
    llm_effort: Literal["low", "medium", "high"] = (
        "medium"  # Haiku 5.5's own default; P3 measures low
    )
    llm_max_tokens: int = 8000  # per reply, thinking included; a final record is ~1k
    llm_timeout_seconds: float = 60.0  # keep LEASE_SECONDS well above this in deployment
    llm_prompt_cache: bool = True  # P3: cache the stable prefix (system + tools + history)
    live_run_budget_usd: float = 0.05  # per live run; a Haiku run is ~$0.006, so ~8x headroom
    # ── Retries + circuit breaker (Day 8) ───────────────────────
    breaker_threshold: int = 5  # consecutive failures before a tool's breaker opens
    # ── Repair ladder (Day 9) ───────────────────────────────────
    repair_attempts: int = 1  # rung-2 re-asks allowed per run (0 = ladder is rungs 0-1 only)
    breaker_open_seconds: float = 30.0  # how long an open breaker waits before one probe
    # Fault injection for proofs only; all default off. Fake external tools only.
    fake_tool_failure_rate: float = 0.0  # chance per call of an injected 503
    fake_tool_down: str = ""  # name of one fake tool that always fails (an outage)
    fake_model_failure_rate: float = 0.0  # chance per model call of an injected 503
    fake_broken_final: str = ""  # Day 9 proofs: break the fake's final answer (see fake_script)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()  # type: ignore[call-arg]  # values come from .env
