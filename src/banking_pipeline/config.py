"""Runtime configuration loaded from env vars / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BANKPIPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Where to look for bank-specific template definitions (regex rules, etc.).
    templates_dir: Path = Field(default=Path("src/banking_pipeline/templates"))

    # Anthropic credentials for the LLM fallback. Leave unset to disable LLM branches.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"

    # Confidence threshold below which classification/extraction falls back to the LLM.
    rule_confidence_threshold: float = 0.75

    # Default currency assumed when a statement omits a currency code.
    default_currency: str = "EUR"


settings = Settings()
