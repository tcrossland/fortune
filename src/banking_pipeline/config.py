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

    # Maps Pictet's printed beneficiary-bank name (the ``Bank`` field on
    # an outgoing ``PAYMENT TRANSACTIONS / Payment`` advice) to the short
    # account-name segment used in beancount cash-leg paths
    # (``Assets:<segment>:<currency>``). Keyed on a substring that
    # uniquely identifies the bank in the Pictet text — e.g. the entry
    # for Revolut matches ``REVOLUT BANK UAB, SUCURSAL EN ESPAN`` and
    # any other Revolut-branded variants.
    #
    # Used by the writer's self-to-self-payment path to route the
    # destination leg to ``Assets:Revolut:<ccy>`` (instead of an elastic
    # ``Expenses:<prefix>:Other`` posting). Override via
    # ``BANKPIPE_BENEFICIARY_BANK_MAP`` env var as a JSON-encoded dict,
    # or by editing this default for project-local conventions.
    beneficiary_bank_map: dict[str, str] = Field(
        default_factory=lambda: {
            "REVOLUT BANK UAB": "Revolut",
        }
    )


settings = Settings()
