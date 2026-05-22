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

  - reportable units = the position held at the fund's ``period_end``;
  - gross ERI = units × ``eri_per_unit``; equalisation = units ×
    ``equalisation_per_unit``; both converted to GBP at the
    ``fund_distribution_date``;
  - taxable income (net) = gross − equalisation;
  - base-cost uplift = the net taxable amount, applied to the pool at
    the distribution date (so it lifts the cost of units still held
    then; units sold earlier already left the pool).
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
from banking_pipeline.tax.uk.currency import to_gbp
from banking_pipeline.tax.uk.section_104 import PoolCostAdjustment
from banking_pipeline.tax.uk.tax_year import tax_year_bounds
from banking_pipeline.writer.builders.security_trade import (
    SECURITY_BUY_TYPES,
    SECURITY_SELL_TYPES,
)

IncomeType = Literal["dividend", "interest"]


class EriEntry(BaseModel):
    """One fund reporting period's excess reportable income.

    ``period_end`` dates the holding used; ``fund_distribution_date``
    (the deemed-income date, typically period end + 6 months) is the
    tax-year and conversion date. Per-unit figures are in ``currency``.
    """

    model_config = ConfigDict(frozen=True)

    isin: str
    period_end: date
    fund_distribution_date: date
    income_type: IncomeType
    eri_per_unit: Decimal
    equalisation_per_unit: Decimal = Decimal(0)
    currency: str

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, value: str) -> str:
        code = normalise_commodity_code(value)
        if code is None:
            raise ValueError(
                f"not a valid ISIN or 11-char commodity ref: {value!r}"
            )
        return code


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
    """Aggregated ERI for one (country, ISIN, income type) in a tax year."""

    country: str
    isin: str
    commodity_name: str
    income_type: str
    gross_gbp: Decimal
    equalisation_gbp: Decimal
    net_gbp: Decimal
    event_count: int


@dataclass
class EriResult:
    rows: list[EriIncomeRow]
    # Base-cost uplift (net taxable ERI) to feed the section 104 pool.
    base_cost_adjustments: dict[str, list[PoolCostAdjustment]] = field(
        default_factory=dict
    )
    # ISINs whose ERI couldn't be converted to GBP (no rate at the
    # distribution date) — excluded so figures aren't silently wrong.
    missing_rate_isins: list[str] = field(default_factory=list)


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

    for isin, entries in eri_entries.items():
        for entry in entries:
            if not (start <= entry.fund_distribution_date <= end):
                continue
            units = _position_as_of(
                entry.period_end, by_isin.get(isin, []), opening.get(isin, [])
            )
            if units <= 0:
                continue
            on = entry.fund_distribution_date
            gross = to_gbp(
                units * entry.eri_per_unit,
                currency=entry.currency, on_date=on, source=source,
            )
            equalisation = to_gbp(
                units * entry.equalisation_per_unit,
                currency=entry.currency, on_date=on, source=source,
            )
            if gross is None or equalisation is None:
                missing.add(isin)
                continue
            net = gross - equalisation

            meta = commodities.get(isin)
            country = (meta.domicile if meta is not None else isin[:2]).upper()
            name = meta.name if meta is not None else ""
            acc.setdefault(
                (country, isin, entry.income_type),
                [Decimal(0), Decimal(0), Decimal(0), 0, name],
            )
            bucket = acc[(country, isin, entry.income_type)]
            bucket[0] += gross
            bucket[1] += equalisation
            bucket[2] += net
            bucket[3] += 1
            adjustments[isin].append(PoolCostAdjustment(on, net))

    rows = [
        EriIncomeRow(
            country=country,
            isin=isin,
            commodity_name=bucket[4],
            income_type=income_type,
            gross_gbp=bucket[0],
            equalisation_gbp=bucket[1],
            net_gbp=bucket[2],
            event_count=bucket[3],
        )
        for (country, isin, income_type), bucket in sorted(acc.items())
    ]
    return EriResult(
        rows=rows,
        base_cost_adjustments=dict(adjustments),
        missing_rate_isins=sorted(missing),
    )
