"""SA106 foreign-income aggregation (dividends).

Groups foreign dividend distributions within a tax year by source
country and ISIN, reporting gross income, foreign withholding tax, and
net — all in GBP — so the figures drop onto SA106 and the WHT supports a
foreign-tax-credit-relief claim. Interest and offshore-income-gains
sections are a deferred follow-up; this is the dividend slice.

"Foreign" excludes GB-domiciled securities (UK dividends belong on
SA100, not SA106). The source country is the withholding jurisdiction
when the advice printed one, else the security's ISIN country prefix.
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
from banking_pipeline.tax.uk.tax_year import tax_year_bounds
from banking_pipeline.writer.builders.dividend import DIVIDEND_TYPES


@dataclass(frozen=True)
class Sa106DividendRow:
    country: str
    isin: str
    commodity_name: str
    gross_gbp: Decimal
    wht_gbp: Decimal
    net_gbp: Decimal
    document_count: int


@dataclass
class Sa106Report:
    dividends: list[Sa106DividendRow]
    missing_rate_isins: list[str] = field(default_factory=list)


def _income_date(tx: Transaction) -> date:
    """The date the income arises — booking/payment date, falling back to
    the ex/trade date."""

    return tx.booking_date or tx.settlement_date or tx.trade_date


def compute_sa106_dividends(
    transactions: list[Transaction],
    *,
    tax_year_label: str,
    commodities: dict[str, CommodityMetadata],
    source: GbpRateSource | None = None,
) -> Sa106Report:
    """Aggregate foreign dividends settling within ``tax_year_label``."""

    start, end = tax_year_bounds(tax_year_label)

    @dataclass
    class _Acc:
        gross: Decimal = Decimal(0)
        wht: Decimal = Decimal(0)
        net: Decimal = Decimal(0)
        count: int = 0
        name: str = ""

    groups: dict[tuple[str, str], _Acc] = defaultdict(_Acc)
    missing: set[str] = set()

    for tx in transactions:
        if tx.document_type not in DIVIDEND_TYPES or not tx.isin:
            continue
        if not (start <= _income_date(tx) <= end):
            continue
        country = (tx.withholding_country or tx.isin[:2]).upper()
        if country == "GB":
            continue  # UK dividend → SA100, not SA106

        on = _income_date(tx)
        gross_native = tx.gross_income if tx.gross_income is not None else tx.amount
        wht_native = tx.withholding_tax if tx.withholding_tax is not None else Decimal(0)
        gross = to_gbp(
            gross_native, currency=tx.currency, on_date=on,
            gbp_rate=tx.gbp_rate, source=source,
        )
        wht = to_gbp(
            wht_native, currency=tx.currency, on_date=on,
            gbp_rate=tx.gbp_rate, source=source,
        )
        net = to_gbp(
            tx.amount, currency=tx.currency, on_date=on,
            gbp_rate=tx.gbp_rate, source=source,
        )
        if gross is None or wht is None or net is None:
            missing.add(tx.isin)
            continue

        acc = groups[(country, tx.isin)]
        acc.gross += gross
        acc.wht += wht
        acc.net += net
        acc.count += 1
        meta = commodities.get(tx.isin)
        if meta is not None:
            acc.name = meta.name

    rows = [
        Sa106DividendRow(
            country=country,
            isin=isin,
            commodity_name=acc.name,
            gross_gbp=acc.gross,
            wht_gbp=acc.wht,
            net_gbp=acc.net,
            document_count=acc.count,
        )
        for (country, isin), acc in sorted(groups.items())
    ]
    return Sa106Report(dividends=rows, missing_rate_isins=sorted(missing))
