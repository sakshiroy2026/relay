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
    tools_provider: Literal["fake"] = "fake"
    fake_delay_seconds: float = 0.0  # Day 6: slow the fake model down so a run can be killed
    # ── Retries + circuit breaker (Day 8) ───────────────────────
    breaker_threshold: int = 5  # consecutive failures before a tool's breaker opens
    breaker_open_seconds: float = 30.0  # how long an open breaker waits before one probe
    # Fault injection for proofs only; all default off. Fake external tools only.
    fake_tool_failure_rate: float = 0.0  # chance per call of an injected 503
    fake_tool_down: str = ""  # name of one fake tool that always fails (an outage)
    fake_model_failure_rate: float = 0.0  # chance per model call of an injected 503
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()  # type: ignore[call-arg]  # values come from .env
