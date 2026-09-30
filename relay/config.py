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
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()  # type: ignore[call-arg]  # values come from .env
