"""Runtime configuration loaded from env vars / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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

    # UK CGT GBP cost-basis sourcing. ``"null"`` (the default) leaves
    # ``Transaction.gbp_rate`` unset so downstream builders behave
    # exactly as before. ``"hmrc-monthly"`` enriches each transaction
    # from HMRC's monthly average rates, read from ``hmrc_rate_path``
    # (or ``data/fx/hmrc-monthly-average.csv`` when unset). See
    # :mod:`banking_pipeline.fx.gbp_rates`.
    gbp_rate_source: Literal["null", "hmrc-monthly"] = "null"
    hmrc_rate_path: Path | None = None

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
            # Substring match against Pictet's printed bank field, so a
            # single ``REVOLUT`` needle covers both Revolut Bank UAB
            # (the licensed bank, named on outgoing wires) and Revolut
            # Payments UAB (the EMI, named on incoming wires from
            # Revolut Pay) — same external pot from the user's
            # perspective.
            "REVOLUT": "Revolut",
        }
    )

    # Counterparty-name → account-segment map. Used by the writer's
    # third-party-payment path (incoming + outgoing wires that aren't
    # self-to-self) to route the elastic counter-leg to a named
    # account instead of the catch-all ``:Other`` placeholder.
    #
    # Lookup is a case-insensitive substring match on Pictet's printed
    # name field — ``Beneficiary`` for outgoing PAYMENT, ``Instructing
    # party`` for INCOMING_PAYMENT, ``Ordenante`` for PAGO_ENTRANTE.
    # The first map entry whose key is a substring of the name wins.
    #
    # The mapped value is the account segment after the family root —
    # the writer prepends ``Income:`` for incoming payments (cash in)
    # and ``Expenses:`` for outgoing (cash out), so a single map entry
    # covers both directions for a counterparty that flows both ways
    # (rare; most are unidirectional).
    #
    # Examples::
    #
    #     {
    #         "ACME EMPLOYER": "External:Earnout:Acme",
    #         "JOHN SMITH LAW FIRM": "External:Legal:Smith",
    #     }
    #
    # ``ACME EMPLOYER`` paying you produces ``Income:External:Earnout:Acme``;
    # paying ``JOHN SMITH LAW FIRM`` produces ``Expenses:External:Legal:Smith``.
    # Override via ``BANKPIPE_COUNTERPARTY_ACCOUNT_MAP`` env var as a
    # JSON-encoded dict, or by editing this default.
    counterparty_account_map: dict[str, str] = Field(
        default_factory=lambda: {
            "AEAT": "External:Tax:AEAT",
            "IBM": "External:Earnout:IBM",
        }
    )


settings = Settings()
