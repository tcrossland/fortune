"""SA106 foreign-dividend aggregation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.tax.uk.sa106 import compute_sa106_dividends


def _div(
    *,
    isin: str,
    amount: Decimal,
    on: date,
    currency: str = "USD",
    gbp_rate: Decimal | None = None,
    gross: Decimal | None = None,
    wht: Decimal | None = None,
    country: str | None = None,
) -> Transaction:
    return Transaction(
        trade_date=on,
        booking_date=on,
        narration="Dividend",
        title="Dividend",
        currency=currency,
        amount=amount,
        isin=isin,
        gbp_rate=gbp_rate,
        gross_income=gross,
        withholding_tax=wht,
        withholding_country=country,
        document_type=DocumentType.DIVIDEND_NOTICE,
        source_path=Path("d.pdf"),
    )


def test_wht_dividend_converted_and_grouped() -> None:
    isin = "US0378331005"
    txs = [
        _div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
             gbp_rate=Decimal("0.80"), gross=Decimal("100.00"),
             wht=Decimal("15.00"), country="US"),
    ]
    report = compute_sa106_dividends(txs, tax_year_label="2025-26", commodities={})
    assert len(report.dividends) == 1
    row = report.dividends[0]
    assert row.country == "US"
    assert row.gross_gbp == Decimal("80.00")
    assert row.wht_gbp == Decimal("12.00")
    assert row.net_gbp == Decimal("68.00")
    assert row.document_count == 1


def _meta(isin: str, *, as_interest: bool) -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin, name="Bond fund", domicile="LU",
        reporting_status="reporting", asset_class="bond",
        first_acquired=date(2020, 1, 1),
        distributions_as_interest=as_interest,
    )


def test_bond_fund_distribution_routed_to_interest() -> None:
    interest_isin = "LU2096759431"
    div_isin = "US0378331005"
    txs = [
        _div(isin=interest_isin, amount=Decimal("400.00"), on=date(2025, 6, 1),
             currency="GBP"),
        _div(isin=div_isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
             gbp_rate=Decimal("0.80"), gross=Decimal("100.00"),
             wht=Decimal("15.00"), country="US"),
    ]
    report = compute_sa106_dividends(
        txs, tax_year_label="2025-26",
        commodities={
            interest_isin: _meta(interest_isin, as_interest=True),
            div_isin: _meta(div_isin, as_interest=False),
        },
    )
    # The flagged fund lands in interest; the equity dividend stays in dividends.
    assert len(report.dividends) == 1
    assert report.dividends[0].isin == div_isin
    assert len(report.interest) == 1
    assert report.interest[0].isin == interest_isin
    assert report.interest[0].gross_gbp == Decimal("400.00")
    assert report.interest[0].country == "LU"


def test_no_interest_flag_leaves_interest_empty() -> None:
    isin = "US0378331005"
    txs = [_div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
                gbp_rate=Decimal("0.80"), country="US")]
    report = compute_sa106_dividends(txs, tax_year_label="2025-26", commodities={})
    assert report.interest == []
    assert len(report.dividends) == 1


def test_multiple_dividends_same_security_aggregate() -> None:
    isin = "US0378331005"
    txs = [
        _div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
             gbp_rate=Decimal("0.80"), gross=Decimal("100.00"),
             wht=Decimal("15.00"), country="US"),
        _div(isin=isin, amount=Decimal("85.00"), on=date(2025, 9, 1),
             gbp_rate=Decimal("0.50"), gross=Decimal("100.00"),
             wht=Decimal("15.00"), country="US"),
    ]
    report = compute_sa106_dividends(txs, tax_year_label="2025-26", commodities={})
    assert len(report.dividends) == 1
    row = report.dividends[0]
    assert row.document_count == 2
    assert row.gross_gbp == Decimal("130.00")  # 80 + 50
    assert row.wht_gbp == Decimal("19.50")  # 12 + 7.5


def test_gb_dividend_excluded() -> None:
    txs = [
        _div(isin="GB00B3VWN518", amount=Decimal("100.00"), on=date(2025, 6, 1),
             currency="GBP"),
    ]
    report = compute_sa106_dividends(txs, tax_year_label="2025-26", commodities={})
    assert report.dividends == []


def test_no_wht_foreign_dividend_included_with_zero_wht() -> None:
    # An offshore fund distribution with no WHT: gross == net, country
    # from the ISIN prefix.
    isin = "LU2096759431"
    txs = [
        _div(isin=isin, amount=Decimal("1242.50"), on=date(2025, 6, 1),
             currency="GBP"),
    ]
    report = compute_sa106_dividends(txs, tax_year_label="2025-26", commodities={})
    assert len(report.dividends) == 1
    row = report.dividends[0]
    assert row.country == "LU"
    assert row.gross_gbp == Decimal("1242.50")
    assert row.wht_gbp == Decimal("0")


def test_dividend_outside_year_excluded() -> None:
    isin = "US0378331005"
    txs = [
        _div(isin=isin, amount=Decimal("85.00"), on=date(2024, 6, 1),
             gbp_rate=Decimal("0.80"), gross=Decimal("100.00"),
             wht=Decimal("15.00"), country="US"),
    ]
    report = compute_sa106_dividends(txs, tax_year_label="2025-26", commodities={})
    assert report.dividends == []


def test_gb_prefix_foreign_situs_income_dropped_but_flagged() -> None:
    # A GB-ISIN depositary receipt over a foreign asset (uk_situs=False):
    # country resolves to GB so it's dropped from SA106 — but because the
    # situs is foreign, the drop is flagged for manual review, not silent.
    isin = "GB00B16KPT44"
    meta = CommodityMetadata(
        isin=isin, name="GDR over foreign asset", domicile="GB",
        reporting_status="reporting", asset_class="equity-fund",
        first_acquired=date(2020, 1, 1), uk_situs=False,
    )
    txs = [_div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
                currency="GBP")]
    report = compute_sa106_dividends(
        txs, tax_year_label="2025-26", commodities={isin: meta}
    )
    assert report.dividends == [] and report.interest == []
    assert report.dropped_uk_situs_foreign == [isin]


def test_gb_domestic_income_dropped_without_flag() -> None:
    # A genuinely UK-domestic GB fund (uk_situs derives True) is dropped and
    # NOT flagged — it correctly belongs on SA100.
    isin = "GB00B16KPT44"
    meta = CommodityMetadata(
        isin=isin, name="UK fund", domicile="GB",
        reporting_status="uk-domestic", asset_class="equity-fund",
        first_acquired=date(2020, 1, 1),
    )
    txs = [_div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
                currency="GBP")]
    report = compute_sa106_dividends(
        txs, tax_year_label="2025-26", commodities={isin: meta}
    )
    assert report.dropped_uk_situs_foreign == []
