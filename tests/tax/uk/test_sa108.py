"""SA108 capital-gains aggregation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.tax.uk.sa108 import compute_sa108


def _tx(
    *,
    doc_type: DocumentType,
    isin: str,
    qty: Decimal,
    amount: Decimal,
    on: date,
    currency: str = "GBP",
    gbp_rate: Decimal | None = None,
    accrued: Decimal | None = None,
) -> Transaction:
    return Transaction(
        trade_date=on,
        narration="trade",
        currency=currency,
        amount=amount,
        isin=isin,
        quantity=qty,
        gbp_rate=gbp_rate,
        accrued_interest=accrued,
        document_type=doc_type,
        source_path=Path("t.pdf"),
    )


def _reporting(isin: str, name: str = "Fund") -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin,
        name=name,
        domicile="IE",
        reporting_status="reporting",
        asset_class="equity-etf",
        first_acquired=date(2018, 1, 1),
    )


def test_basic_pool_disposal_gain_in_year() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.reporting_status == "reporting"
    assert row.gain_gbp == Decimal("500.00")
    assert row.match_type == "s104"
    assert row.commodity_name == "Fund"


def test_disposal_outside_year_excluded() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2023, 5, 1)),
        # Disposed in 2024-25, not 2025-26.
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2024, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert report.rows == []


def test_unknown_metadata_tagged_unknown() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("50"),
            amount=Decimal("-500"), on=date(2024, 1, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-50"),
            amount=Decimal("400"), on=date(2025, 7, 1)),
    ]
    report = compute_sa108(txs, tax_year_label="2025-26", commodities={})
    assert len(report.rows) == 1
    assert report.rows[0].reporting_status == "unknown"
    assert report.rows[0].gain_gbp == Decimal("-100.00")  # a loss


def test_accrued_interest_excluded_from_consideration() -> None:
    isin = "DE000BU3Z005"
    txs = [
        # Net cash -1100 includes -100 accrued → capital cost is 1000.
        _tx(doc_type=DocumentType.BUY_BONDS, isin=isin, qty=Decimal("1000"),
            amount=Decimal("-1100"), on=date(2024, 5, 1), accrued=Decimal("-100")),
        # Net cash 1600 includes 100 accrued → capital proceeds are 1500.
        _tx(doc_type=DocumentType.SELL_BONDS, isin=isin, qty=Decimal("-1000"),
            amount=Decimal("1600"), on=date(2025, 6, 1), accrued=Decimal("100")),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert report.rows[0].cost_gbp == Decimal("1000.00")
    assert report.rows[0].proceeds_gbp == Decimal("1500.00")
    assert report.rows[0].gain_gbp == Decimal("500.00")


def test_foreign_currency_uses_gbp_rate() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1),
            currency="USD", gbp_rate=Decimal("0.80")),  # cost 800 GBP
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1),
            currency="USD", gbp_rate=Decimal("0.90")),  # proceeds 1350 GBP
    ]
    report = compute_sa108(txs, tax_year_label="2025-26", commodities={})
    assert report.rows[0].cost_gbp == Decimal("800.00")
    assert report.rows[0].proceeds_gbp == Decimal("1350.00")
    assert report.rows[0].gain_gbp == Decimal("550.00")


def test_missing_rate_excludes_isin() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1), currency="USD"),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1), currency="USD"),
    ]
    report = compute_sa108(txs, tax_year_label="2025-26", commodities={})
    assert report.rows == []
    assert report.missing_rate_isins == [isin]
