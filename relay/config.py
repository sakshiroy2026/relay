from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    dev_tenant_id: str
    lease_seconds: int = 90
    # ── LLM (Day 4) ─────────────────────────────────────────────
    llm_provider: Literal["fake", "anthropic"] = "fake"
    model_planner: str = "fake-planner"
    model_cheap: str = "fake-cheap"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()  # type: ignore[call-arg]  # values come from .env
