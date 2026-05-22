"""SA108 capital-gains aggregation from sidecar transactions.

Builds per-ISIN acquisitions and disposals out of the security trades in
the JSONL sidecars, runs the section 104 matcher over the full history,
and reports the disposals that fall in the requested tax year — tagged
with the security's reporting status so the caller can route
reporting / uk-domestic gains to SA108 and flag non-reporting (offshore
income gains) and unclassified holdings.

CGT consideration excludes accrued bond interest: Pictet's net cash
bundles the accrued-interest leg in, but that's interest income (handled
elsewhere), not capital, so we subtract it from both cost and proceeds.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.models import Transaction
from banking_pipeline.tax.uk.currency import to_gbp
from banking_pipeline.tax.uk.section_104 import (
    Acquisition,
    Disposal,
    match_disposals,
)
from banking_pipeline.tax.uk.tax_year import tax_year_bounds
from banking_pipeline.writer.builders.security_trade import (
    SECURITY_BUY_TYPES,
    SECURITY_SELL_TYPES,
)


@dataclass(frozen=True)
class Sa108Row:
    """One matched disposal portion within the tax year."""

    disposal_date: date
    isin: str
    commodity_name: str
    reporting_status: str
    quantity: Decimal
    proceeds_gbp: Decimal
    cost_gbp: Decimal
    gain_gbp: Decimal
    match_type: str
    acquisition_dates: list[date]
    # CGT rate-change bucket: ``"pre"`` / ``"post"`` relative to the tax
    # year's rate-change date, or ``""`` when the year has no split.
    period: str = ""


@dataclass
class Sa108Report:
    rows: list[Sa108Row]
    # ISINs excluded because a trade couldn't be converted to GBP (no
    # per-transaction rate and none from the rate source) — emitting a
    # half-converted pool would be worse than flagging the gap.
    missing_rate_isins: list[str] = field(default_factory=list)


def _consideration_native(tx: Transaction) -> Decimal:
    """Capital consideration in the trade currency: net cash less any
    accrued interest (which is interest income, not capital)."""

    accrued = abs(tx.accrued_interest) if tx.accrued_interest is not None else Decimal(0)
    return abs(tx.amount) - accrued


def _period(disposal_date: date, rate_change_date: date | None) -> str:
    """CGT rate bucket for a disposal date: ``"pre"`` / ``"post"``, or
    ``""`` when the tax year has no mid-year rate change."""

    if rate_change_date is None:
        return ""
    return "pre" if disposal_date < rate_change_date else "post"


def compute_sa108(
    transactions: list[Transaction],
    *,
    tax_year_label: str,
    commodities: dict[str, CommodityMetadata],
    source: GbpRateSource | None = None,
    rate_change_date: date | None = None,
) -> Sa108Report:
    """Compute SA108 disposal rows for ``tax_year_label``.

    ``transactions`` should span the full available history (the section
    104 pool is cumulative); only disposals settling within the tax year
    are returned. ``source`` supplies GBP rates for any transaction the
    extractor didn't already stamp with ``gbp_rate``. ``rate_change_date``
    (the year's mid-year CGT rate change, e.g. 2024-10-30 for 2024-25)
    tags each row's ``period`` so disposals can be split before / on-or-
    after it; ``None`` leaves ``period`` empty.
    """

    start, end = tax_year_bounds(tax_year_label)

    by_isin: dict[str, list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.isin and tx.document_type in (SECURITY_BUY_TYPES | SECURITY_SELL_TYPES):
            by_isin[tx.isin].append(tx)

    rows: list[Sa108Row] = []
    missing: list[str] = []

    for isin, txs in by_isin.items():
        acqs: list[Acquisition] = []
        disps: list[Disposal] = []
        unconverted = False
        for tx in txs:
            if tx.quantity is None:
                continue
            gbp = to_gbp(
                _consideration_native(tx),
                currency=tx.currency,
                on_date=tx.trade_date,
                gbp_rate=tx.gbp_rate,
                source=source,
            )
            if gbp is None:
                unconverted = True
                break
            qty = abs(tx.quantity)
            if tx.document_type in SECURITY_BUY_TYPES:
                acqs.append(Acquisition(tx.trade_date, qty, gbp))
            else:
                disps.append(Disposal(tx.trade_date, qty, gbp))

        if unconverted:
            missing.append(isin)
            continue

        meta = commodities.get(isin)
        status = meta.reporting_status if meta is not None else "unknown"
        name = meta.name if meta is not None else ""
        for m in match_disposals(acqs, disps):
            if not (start <= m.disposal_date <= end):
                continue
            rows.append(
                Sa108Row(
                    disposal_date=m.disposal_date,
                    isin=isin,
                    commodity_name=name,
                    reporting_status=status,
                    quantity=m.disposal_qty,
                    proceeds_gbp=m.proceeds_gbp,
                    cost_gbp=m.cost_gbp,
                    gain_gbp=m.gain_gbp,
                    match_type=m.matched_against,
                    acquisition_dates=m.acquisition_dates,
                    period=_period(m.disposal_date, rate_change_date),
                )
            )

    rows.sort(key=lambda r: (r.disposal_date, r.isin))
    missing.sort()
    return Sa108Report(rows=rows, missing_rate_isins=missing)
