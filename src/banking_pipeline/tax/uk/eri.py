"""Excess reportable income (ERI) and equalisation for UK reporting funds.

UK reporting funds that accumulate (rather than distribute) report
*excess reportable income* — income deemed to arise to holders at the
fund's reporting-period end, taxable as dividend or interest. ERI is
also added to the CGT base cost on a later disposal (you've already been
taxed on it). *Income equalisation* on units acquired during the period
is a return of capital: it reduces both the taxable income and the base
cost.

ERI figures aren't on the Pictet trade advices — the funds publish them
— so they come from a user-maintained ``data/eri.toml`` table. This
module turns that table plus the ledger holdings into per-fund income
(split dividend/interest) and the section 104 base-cost adjustments.

Model (documented because it's tax-critical):

  - reportable units = the position held at the fund's reporting period
    end. UK offshore-funds rules deem the income to arise six months
    later (the ``fund_distribution_date``, the tax point that fund/
    custodian reports display); the units are still measured at the
    period end six months before. So ``period_end`` defaults to
    ``fund_distribution_date`` minus six months (month end) and need
    only be given explicitly to override that for an unusual fund;
  - gross ERI = units × ``eri_per_unit``; equalisation = units ×
    ``equalisation_per_unit``; both converted to GBP at the
    ``fund_distribution_date``;
  - taxable income = the gross ERI. Equalisation is *not* deducted from
    the income — this matches how fund and custodian tax reports (e.g.
    Pictet's "amount received") present the figure to declare;
  - base-cost adjustment = gross − equalisation, applied to the pool at
    the distribution date: the taxed gross is added to base cost (so a
    later disposal isn't taxed again on it) and the equalisation, a
    return of capital, is taken back off. It lifts the cost of units
    still held then; units sold earlier already left the pool.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from banking_pipeline.commodities_metadata import (
    CommodityMetadata,
    normalise_commodity_code,
)
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.models import Transaction
from banking_pipeline.opening_positions import OpeningLot
from banking_pipeline.tax.uk.currency import RateGap, to_gbp_all
from banking_pipeline.tax.uk.section_104 import PoolCostAdjustment
from banking_pipeline.tax.uk.tax_year import (
    date_to_tax_year,
    reporting_period_end,
    tax_year_bounds,
)
from banking_pipeline.writer.builders.security_trade import (
    SECURITY_BUY_TYPES,
    SECURITY_SELL_TYPES,
)

IncomeType = Literal["dividend", "interest"]


class EriEntry(BaseModel):
    """One fund reporting period's excess reportable income.

    ``fund_distribution_date`` is the deemed-income date (the tax point
    fund/custodian reports display) and the tax-year and FX-conversion
    date. The reportable holding is measured at the fund's reporting
    period end — six months earlier — exposed as :attr:`measurement_date`.
    Leave ``period_end`` unset to derive it as ``fund_distribution_date``
    minus six months (month end); set it only to override that for a
    fund whose period-to-distribution gap isn't the regulatory six
    months. Per-unit figures are in ``currency``.
    """

    model_config = ConfigDict(frozen=True)

    isin: str
    fund_distribution_date: date
    income_type: IncomeType
    eri_per_unit: Decimal
    equalisation_per_unit: Decimal = Decimal(0)
    currency: str
    period_end: date | None = None

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, value: str) -> str:
        code = normalise_commodity_code(value)
        if code is None:
            raise ValueError(
                f"not a valid ISIN or 11-char commodity ref: {value!r}"
            )
        return code

    @property
    def measurement_date(self) -> date:
        """The date the reportable holding is measured (period end)."""

        if self.period_end is not None:
            return self.period_end
        return reporting_period_end(self.fund_distribution_date)


def load_eri(path: Path) -> dict[str, list[EriEntry]]:
    """Parse ``path`` into ``{isin: [EriEntry, ...]}``."""

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, list[EriEntry]] = defaultdict(list)
    for entry in raw.get("eri", []):
        model = EriEntry.model_validate(entry)
        entries[model.isin].append(model)
    return dict(entries)


@dataclass(frozen=True)
class EriIncomeRow:
    """Aggregated ERI for one (country, ISIN, income type) in a tax year.

    ``gross_gbp`` is the taxable income (equalisation is not netted off);
    ``base_cost_adjustment_gbp`` (gross − equalisation) is the section
    104 pool uplift.
    """

    country: str
    isin: str
    commodity_name: str
    income_type: str
    gross_gbp: Decimal
    equalisation_gbp: Decimal
    base_cost_adjustment_gbp: Decimal
    event_count: int


@dataclass
class EriResult:
    rows: list[EriIncomeRow]
    # Base-cost uplift (gross ERI less equalisation) for the section 104 pool.
    base_cost_adjustments: dict[str, list[PoolCostAdjustment]] = field(
        default_factory=dict
    )
    # ISINs whose ERI couldn't be converted to GBP (no rate at the
    # distribution date) — excluded so figures aren't silently wrong.
    missing_rate_isins: list[str] = field(default_factory=list)
    # The same gaps with currency/month detail (which HMRC CSV row to add).
    missing_rates: list[RateGap] = field(default_factory=list)
    # ISINs whose typed eri.toml ``income_type`` was overridden to interest
    # because the commodity is a bond fund (``distributions_as_interest``);
    # surfaced so the inconsistent eri.toml entry gets corrected.
    reclassified_to_interest: list[str] = field(default_factory=list)


def cumulative_base_cost_adjustments(
    transactions: list[Transaction],
    *,
    eri_entries: dict[str, list[EriEntry]],
    commodities: dict[str, CommodityMetadata],
    opening_positions: dict[str, list[OpeningLot]] | None = None,
    source: GbpRateSource | None = None,
) -> tuple[dict[str, list[PoolCostAdjustment]], list[RateGap]]:
    """Section 104 base-cost adjustments from ERI across the **full** history.

    :func:`compute_eri` scopes to one tax year, but the section 104 pool is
    cumulative: a *current* cost basis needs the ERI uplifts from every year,
    not one. This runs ``compute_eri`` for each distinct tax year the ``eri``
    table spans (keyed by each entry's ``fund_distribution_date``) and merges
    the per-ISIN adjustment lists, so the holdings lens can uplift the pool by
    the whole accumulated ERI. Returns the merged adjustments and any GBP-rate
    gaps encountered (so the caller can surface an incomplete uplift).
    """

    years = sorted(
        {
            date_to_tax_year(entry.fund_distribution_date)
            for entries in eri_entries.values()
            for entry in entries
        }
    )
    merged: dict[str, list[PoolCostAdjustment]] = defaultdict(list)
    gaps: list[RateGap] = []
    for year in years:
        result = compute_eri(
            transactions,
            tax_year_label=year,
            eri_entries=eri_entries,
            commodities=commodities,
            opening_positions=opening_positions,
            source=source,
        )
        for isin, adjustments in result.base_cost_adjustments.items():
            merged[isin].extend(adjustments)
        gaps.extend(result.missing_rates)
    return dict(merged), gaps


def _position_as_of(
    on_date: date, txs: list[Transaction], opening_lots: list[OpeningLot]
) -> Decimal:
    """Units held on ``on_date``: opening lots + signed ledger quantities
    (buys positive, sells negative) up to and including the date."""

    qty = sum(
        (lot.quantity for lot in opening_lots if lot.acquired <= on_date),
        Decimal(0),
    )
    for tx in txs:
        if tx.quantity is not None and tx.trade_date <= on_date:
            qty += tx.quantity
    return qty


def compute_eri(
    transactions: list[Transaction],
    *,
    tax_year_label: str,
    eri_entries: dict[str, list[EriEntry]],
    commodities: dict[str, CommodityMetadata],
    opening_positions: dict[str, list[OpeningLot]] | None = None,
    source: GbpRateSource | None = None,
) -> EriResult:
    """Compute ERI income and base-cost adjustments for ``tax_year_label``."""

    start, end = tax_year_bounds(tax_year_label)
    opening = opening_positions or {}

    by_isin: dict[str, list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.isin and tx.document_type in (SECURITY_BUY_TYPES | SECURITY_SELL_TYPES):
            by_isin[tx.isin].append(tx)

    # (country, isin, income_type) -> [gross, equalisation, net, count, name]
    acc: dict[tuple[str, str, str], list] = {}  # type: ignore[type-arg]
    adjustments: dict[str, list[PoolCostAdjustment]] = defaultdict(list)
    missing: set[str] = set()
    gaps: set[RateGap] = set()
    reclassified: set[str] = set()

    for isin, entries in eri_entries.items():
        for entry in entries:
            if not (start <= entry.fund_distribution_date <= end):
                continue
            units = _position_as_of(
                entry.measurement_date, by_isin.get(isin, []), opening.get(isin, [])
            )
            if units <= 0:
                continue
            on = entry.fund_distribution_date
            converted = to_gbp_all(
                [units * entry.eri_per_unit, units * entry.equalisation_per_unit],
                currency=entry.currency, on_date=on, source=source,
            )
            if converted is None:
                missing.add(isin)
                gaps.add(RateGap.at(isin, entry.currency, on))
                continue
            gross, equalisation = converted
            # Taxable income is the gross; the pool uplift is net of
            # equalisation (return of capital).
            base_cost_adj = gross - equalisation

            meta = commodities.get(isin)
            country = (meta.domicile if meta is not None else isin[:2]).upper()
            name = meta.name if meta is not None else ""
            # The bond-fund rule (``distributions_as_interest``) makes ALL the
            # fund's income — distributions *and* ERI — foreign interest, so
            # ERI follows the commodity flag, not the typed ``income_type``;
            # a typed disagreement is overridden here and flagged for the user.
            income_type = entry.income_type
            if meta is not None and meta.distributions_as_interest:
                if income_type != "interest":
                    reclassified.add(isin)
                income_type = "interest"
            acc.setdefault(
                (country, isin, income_type),
                [Decimal(0), Decimal(0), Decimal(0), 0, name],
            )
            bucket = acc[(country, isin, income_type)]
            bucket[0] += gross
            bucket[1] += equalisation
            bucket[2] += base_cost_adj
            bucket[3] += 1
            adjustments[isin].append(PoolCostAdjustment(on, base_cost_adj))

    rows = [
        EriIncomeRow(
            country=country,
            isin=isin,
            commodity_name=bucket[4],
            income_type=income_type,
            gross_gbp=bucket[0],
            equalisation_gbp=bucket[1],
            base_cost_adjustment_gbp=bucket[2],
            event_count=bucket[3],
        )
        for (country, isin, income_type), bucket in sorted(acc.items())
    ]
    return EriResult(
        rows=rows,
        base_cost_adjustments=dict(adjustments),
        missing_rate_isins=sorted(missing),
        missing_rates=sorted(gaps, key=lambda g: (g.isin, g.month)),
        reclassified_to_interest=sorted(reclassified),
    )
