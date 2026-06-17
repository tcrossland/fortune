"""Runtime configuration loaded from env vars / .env."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from banking_pipeline.tax.uk.rates import (
    CgtRateSchedule,
    IncomeTaxBands,
    default_cgt_rates,
    default_income_bands,
)

# Personal / structured runtime config lives in the ``[settings]`` table of
# this file — the same TOML that drives ``rebuild`` (its other tables are
# the BatchConfig schema, which ``load_config`` reads and which ignores
# ``[settings]``). Keeps keyed maps (counterparty / beneficiary routing,
# tax tables) as readable TOML rather than JSON-in-env. Env vars
# (``BANKPIPE_*`` / ``.env``) still override it; secrets stay env-only.
_CONFIG_TOML = Path("banking-pipeline.toml")


class _SettingsTomlSource(TomlConfigSettingsSource):
    """Reads only the ``[settings]`` table of ``banking-pipeline.toml``.

    The rest of the file is the rebuild ``BatchConfig``; scoping to the
    sub-table keeps the two schemas from colliding. A missing file or
    missing table contributes no values (env / defaults stand).
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls, toml_file=_CONFIG_TOML)

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        if not file_path.is_file():
            return {}
        section = super()._read_file(file_path).get("settings", {})
        return section if isinstance(section, dict) else {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BANKPIPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence (first wins): explicit init args, then env vars, then
        # ``.env``, then the ``[settings]`` TOML table, then field defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _SettingsTomlSource(settings_cls),
            file_secret_settings,
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

    # Hand-curated UK-tax commodity metadata (ISIN → domicile,
    # reporting status, asset class). Consumed by ``portfolio`` /
    # ``rebuild`` to emit beancount ``commodity`` directives. Defaults
    # to ``data/commodities.toml`` when that file exists, else unset
    # (no commodity directives emitted). See
    # :mod:`banking_pipeline.commodities_metadata`.
    commodities_metadata_path: Path | None = Field(
        default_factory=lambda: (
            Path("data/commodities.toml")
            if Path("data/commodities.toml").is_file()
            else None
        )
    )

    # Root for ``tax-report`` CSV output; the command writes a
    # ``<year>/`` subdirectory under it.
    tax_reports_dir: Path = Path("reports/uk-tax")

    # Output directory for ``reconcile`` (statement-balance vs.
    # ledger-computed-balance diff): writes ``summary.txt`` and
    # ``drift.csv`` here.
    reconciliation_dir: Path = Path("reports/reconciliation")

    # Output directory for ``concentration`` (portfolio exposure
    # breakdown): writes ``concentration.md`` and ``holdings.csv`` here.
    concentration_reports_dir: Path = Path("reports/concentration")

    # Output directory for ``net-worth`` (net-worth-over-time): writes
    # ``net-worth.md`` and ``net-worth.csv`` here.
    net_worth_reports_dir: Path = Path("reports/net-worth")

    # Output directory for ``income`` (income-by-source): writes
    # ``income.md`` and ``income.csv`` here.
    income_reports_dir: Path = Path("reports/income")

    # Output directory for ``allocation`` (asset-allocation-over-time):
    # writes ``allocation.md`` and ``allocation.csv`` here.
    allocation_reports_dir: Path = Path("reports/allocation")

    # Output directory for ``portfolio-allocation`` (per-portfolio
    # breakdown): writes ``portfolio-allocation.md`` and
    # ``portfolio-allocation.csv`` here.
    portfolio_allocation_reports_dir: Path = Path("reports/portfolio-allocation")

    # Output directory for ``trial-balance``: writes ``trial-balance.md``
    # and ``trial-balance.csv`` here.
    trial_balance_reports_dir: Path = Path("reports/trial-balance")

    # Output directory for ``mandate-scorecard``: writes
    # ``mandate-scorecard.md`` and ``.csv`` here.
    mandate_scorecard_reports_dir: Path = Path("reports/mandate-scorecard")

    # Pre-ledger / transferred-in opening positions (ISIN → lots with a
    # GBP cost) seeded into the section 104 pool. Defaults to
    # ``data/opening-positions.toml`` when present. See
    # :mod:`banking_pipeline.opening_positions`.
    opening_positions_path: Path | None = Field(
        default_factory=lambda: (
            Path("data/opening-positions.toml")
            if Path("data/opening-positions.toml").is_file()
            else None
        )
    )

    # Residential property held off the investment ledger. Brought onto
    # the ledger by ``banking-pipeline property`` and folded into the
    # ``concentration`` / ``net-worth`` reports. Defaults to
    # ``data/property.toml`` when present. See
    # :mod:`banking_pipeline.property`.
    property_path: Path | None = Field(
        default_factory=lambda: (
            Path("data/property.toml")
            if Path("data/property.toml").is_file()
            else None
        )
    )

    # Output file for ``banking-pipeline property`` (generated property
    # ledger, included by ``main.beancount``).
    property_ledger_path: Path = Path("data/property.beancount")

    # Defaults for ``banking-pipeline import`` (the first pipeline stage:
    # file raw downloads into a dated tree). ``import_archive_dir`` is the
    # archive root the command files into as
    # ``<root>/<year>/<account>/<YYYYMMDD>-<reference>.pdf``.
    #
    # The source is resolved in order: an explicit ``SOURCE`` argument, else
    # ``import_source_glob`` (a glob — ``~`` allowed — selecting the import
    # sources, typically the bank's periodic zip downloads, e.g.
    # ``~/Downloads/files-*.zip``; every match is a source and they're filed
    # as one batch, so a reference shared across two zips is disambiguated),
    # else ``import_source_dir`` (a single incoming download folder or zip).
    # When none is set the command errors. See
    # :mod:`banking_pipeline.archive`.
    import_source_glob: str | None = None
    import_source_dir: Path | None = None
    import_archive_dir: Path | None = None

    # Excess reportable income (ERI) table for accumulating reporting
    # funds (per-unit deemed income + equalisation, by fund period).
    # Defaults to ``data/eri.toml`` when present. See
    # :mod:`banking_pipeline.tax.uk.eri`.
    eri_path: Path | None = Field(
        default_factory=lambda: (
            Path("data/eri.toml") if Path("data/eri.toml").is_file() else None
        )
    )

    # CGT main-rate change dates by tax-year label. From this date the
    # rate changed mid-year, so HMRC requires disposals split before /
    # on-or-after it. A year with no entry is reported without a split.
    cgt_rate_change_dates: dict[str, date] = Field(
        default_factory=lambda: {"2024-25": date(2024, 10, 30)}
    )

    # CGT annual exempt amount (tax-free allowance) by tax-year label, in
    # GBP. Statutory values; the £12,300 allowance was cut to £6,000 for
    # 2023-24 and £3,000 from 2024-25 (frozen since). A year missing here
    # is treated as a zero allowance and flagged in the tax-report
    # summary, so add new years as HMRC sets them. Consumed by
    # :mod:`banking_pipeline.tax.uk.cgt_allowance`.
    cgt_annual_exempt_amount: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "2020-21": Decimal("12300"),
            "2021-22": Decimal("12300"),
            "2022-23": Decimal("12300"),
            "2023-24": Decimal("6000"),
            "2024-25": Decimal("3000"),
            "2025-26": Decimal("3000"),
            "2026-27": Decimal("3000"),
        }
    )

    # Income-tax bands / rates and CGT rate percentages by tax-year label,
    # consumed only by the ``tax-forecast`` command to turn SA108/SA106
    # amounts into an estimated liability. Statutory England/Wales/NI
    # defaults live in :mod:`banking_pipeline.tax.uk.rates`; add a new
    # year there (or override here) as HMRC sets it. A year missing from
    # ``income_tax_bands`` makes the forecast abort with a clear error
    # rather than guess. ``cgt_forecast_rates`` carries the rate
    # *percentages*; the rate-change *date* and the AEA stay in
    # ``cgt_rate_change_dates`` / ``cgt_annual_exempt_amount`` above.
    income_tax_bands: dict[str, IncomeTaxBands] = Field(
        default_factory=default_income_bands
    )
    cgt_forecast_rates: dict[str, CgtRateSchedule] = Field(
        default_factory=default_cgt_rates
    )

    # Date the user became UK tax resident (a split-year arrival date).
    # Income and gains arising *before* this date are not UK-taxable (the
    # non-resident / overseas part of a split year), and tax years wholly
    # before it are dropped from the UK reports entirely. ``None`` (the
    # default) assumes UK residence throughout — today's behaviour,
    # unchanged. The 10-prior-non-resident-years FIG eligibility test
    # can't be derived from the ledger; configuring this asserts it.
    uk_residence_start_date: date | None = None

    # Tax years for which the 4-year Foreign Income & Gains (FIG) regime
    # is claimed (e.g. ``{"2025-26"}``). A claim relieves foreign income
    # and non-UK gains to nil but forfeits the personal allowance and the
    # CGT annual exempt amount for that year. Elective and annual — only
    # the first four UK-resident tax years (and none before 2025-26) are
    # eligible; a claim outside that window is flagged.
    fig_claim_years: frozenset[str] = Field(default_factory=frozenset)

    # Pre-ledger allowable capital losses carried into the earliest ledger
    # tax year (the loss-carry-forward chain seeds its pool with this).
    # Defaults to ``data/cgt-losses.toml`` when present; the real file is
    # gitignored. See :mod:`banking_pipeline.cgt_losses`.
    cgt_losses_path: Path | None = Field(
        default_factory=lambda: (
            Path("data/cgt-losses.toml")
            if Path("data/cgt-losses.toml").is_file()
            else None
        )
    )

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
    # ``Expenses:<prefix>:Other`` posting). Set under
    # ``[settings.beneficiary_bank_map]`` in ``banking-pipeline.toml`` (or
    # override via the ``BANKPIPE_BENEFICIARY_BANK_MAP`` env var as a
    # JSON-encoded dict).
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
    # Set under ``[settings.counterparty_account_map]`` in
    # ``banking-pipeline.toml`` (readable TOML), e.g.::
    #
    #     [settings.counterparty_account_map]
    #     "ACME EMPLOYER" = "External:Earnout:Acme"
    #     "JOHN SMITH LAW FIRM" = "External:Legal:Smith"
    #
    # ``ACME EMPLOYER`` paying you produces ``Income:External:Earnout:Acme``;
    # paying ``JOHN SMITH LAW FIRM`` produces ``Expenses:External:Legal:Smith``.
    # The ``BANKPIPE_COUNTERPARTY_ACCOUNT_MAP`` env var (JSON dict) still
    # overrides the TOML if set.
    counterparty_account_map: dict[str, str] = Field(
        default_factory=lambda: {
            "AEAT": "External:Tax:AEAT",
            "IBM": "External:Earnout:IBM",
        }
    )


settings = Settings()
