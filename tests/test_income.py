"""Income-by-source aggregation (dividends + interest received)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.income import compute_income, render_markdown
from banking_pipeline.models import DocumentType, Transaction


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
    wrapper: str | None = None,
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
        account_wrapper=wrapper,
        document_type=DocumentType.DIVIDEND_NOTICE,
        source_path=Path("d.pdf"),
    )


def _interest(
    *,
    amount: Decimal,
    on: date,
    currency: str = "GBP",
    doc_type: DocumentType = DocumentType.INTEREST_PAYMENT,
    narration: str = "Interest",
    wrapper: str | None = None,
) -> Transaction:
    return Transaction(
        trade_date=on,
        booking_date=on,
        narration=narration,
        title="Interest",
        currency=currency,
        amount=amount,
        account_wrapper=wrapper,
        document_type=doc_type,
        source_path=Path("i.pdf"),
    )


def _meta(isin: str, *, as_interest: bool, name: str = "Fund") -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin, name=name, domicile="LU",
        reporting_status="reporting", asset_class="bond",
        first_acquired=date(2020, 1, 1),
        distributions_as_interest=as_interest,
    )


def test_dividend_grouped_with_wht_in_gbp() -> None:
    isin = "US0378331005"
    txs = [
        _div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
             gbp_rate=Decimal("0.80"), gross=Decimal("100.00"),
             wht=Decimal("15.00"), country="US"),
    ]
    report = compute_income(txs, period="tax-year", commodities={})
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.kind == "dividend"
    assert row.period == "2025-26"
    assert row.gross_gbp == Decimal("80.00")
    assert row.wht_gbp == Decimal("12.00")
    assert row.net_gbp == Decimal("68.00")
    assert row.count == 1
    assert row.wrapper is None


def test_bond_fund_distribution_reclassified_to_interest() -> None:
    isin = "LU2096759431"
    txs = [_div(isin=isin, amount=Decimal("400.00"), on=date(2025, 6, 1),
                currency="GBP")]
    report = compute_income(
        txs, period="tax-year",
        commodities={isin: _meta(isin, as_interest=True, name="Bond fund")},
    )
    assert len(report.rows) == 1
    assert report.rows[0].kind == "interest"
    assert report.rows[0].source_name == "Bond fund"


def test_cash_interest_received_counted_but_paid_excluded() -> None:
    txs = [
        # Credit-balance interest paid to the user — income.
        _interest(amount=Decimal("12.50"), on=date(2025, 5, 1)),
        # Overdraft interest the user pays — an expense, not income.
        _interest(amount=Decimal("-30.00"), on=date(2025, 5, 1)),
    ]
    report = compute_income(txs, period="tax-year", commodities={})
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.kind == "interest"
    assert row.net_gbp == Decimal("12.50")
    assert row.count == 1


def test_isa_income_included_and_flagged() -> None:
    txs = [
        _interest(amount=Decimal("0.19"), on=date(2025, 5, 1),
                  doc_type=DocumentType.VANGUARD_REGULAR_STATEMENT,
                  narration="Cash Account Interest", wrapper="isa"),
        # A deposit (contribution) on the same statement type is not income.
        _interest(amount=Decimal("500.00"), on=date(2025, 5, 1),
                  doc_type=DocumentType.VANGUARD_REGULAR_STATEMENT,
                  narration="Deposit", wrapper="isa"),
    ]
    report = compute_income(txs, period="tax-year", commodities={})
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.kind == "interest"
    assert row.wrapper == "isa"
    assert row.net_gbp == Decimal("0.19")


def test_period_mode_tax_year_vs_calendar() -> None:
    # 1 Feb 2026 is calendar 2026 but tax year 2025-26.
    isin = "GB00B16KPT44"
    txs = [_div(isin=isin, amount=Decimal("50.00"), on=date(2026, 2, 1),
                currency="GBP")]

    ty = compute_income(txs, period="tax-year", commodities={})
    assert ty.rows[0].period == "2025-26"

    cal = compute_income(txs, period="calendar", commodities={})
    assert cal.rows[0].period == "2026"


def test_missing_rate_excluded_and_recorded() -> None:
    isin = "US0378331005"
    # No per-tx gbp_rate and no rate source → unconvertible, so dropped.
    txs = [_div(isin=isin, amount=Decimal("85.00"), on=date(2025, 6, 1),
                currency="USD")]
    report = compute_income(txs, period="tax-year", commodities={}, source=None)
    assert report.rows == []
    assert len(report.missing_rates) == 1
    gap = report.missing_rates[0]
    assert gap.currency == "USD"
    assert gap.month == "2025-06"


def test_render_markdown_totals_and_tax_free_note() -> None:
    txs = [
        _div(isin="GB00B16KPT44", amount=Decimal("50.00"), on=date(2025, 6, 1),
             currency="GBP"),
        _interest(amount=Decimal("0.19"), on=date(2025, 6, 1),
                  doc_type=DocumentType.VANGUARD_REGULAR_STATEMENT,
                  narration="Cash Account Interest", wrapper="isa"),
    ]
    md = render_markdown(compute_income(txs, period="tax-year", commodities={}))
    assert "# Income by source" in md
    assert "## Totals by period" in md
    assert "tax-free (ISA)" in md
    assert "2025-26" in md


def test_empty_report_renders_placeholder() -> None:
    md = render_markdown(compute_income([], period="tax-year", commodities={}))
    assert "No dividend or interest income found." in md
