"""Mandate cost scorecard (step 1): bucketing, GBP conversion, rendering.

The bean-query subprocess is not exercised — `build_cost_report` takes a
`QueryResult`, so the pure logic (parse, bucket, convert, denominator,
render) is tested with synthetic rows. A `FakeRates` stands in for the GBP
rate source; a hand-built `NetWorthTimeline` supplies the average-invested
denominator.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline import mandate_scorecard as ms
from banking_pipeline.bean_query import QueryResult
from banking_pipeline.net_worth import NetWorthPoint, NetWorthTimeline


class FakeRates:
    """Structural GbpRateSource: fixed EUR/USD rates, nothing else (so JPY
    has no rate and produces a gap)."""

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return {"EUR": Decimal("0.85"), "USD": Decimal("0.75")}.get(
            currency.upper()
        )


def _point(on_date: date, gross: str) -> NetWorthPoint:
    return NetWorthPoint(
        on_date=on_date,
        gross_long_gbp=Decimal(gross),
        net_cash_gbp=Decimal(0),
        net_worth_gbp=Decimal(gross),
        change_gbp=None,
        portfolios=1,
    )


def _timeline() -> NetWorthTimeline:
    # Two 2024 snapshots averaging to 1,000,000; one 2025 snapshot.
    return NetWorthTimeline(
        points=(
            _point(date(2024, 3, 31), "800000"),
            _point(date(2024, 9, 30), "1200000"),
            _point(date(2025, 3, 31), "2000000"),
        ),
        rate_gaps=(),
        missing_prices=(),
        unclassified=(),
    )


# --- _parse_amount --------------------------------------------------------


def test_parse_amount_variants() -> None:
    assert ms._parse_amount("100.50 EUR") == (Decimal("100.50"), "EUR")
    # bean-query strips thousands as commas.
    assert ms._parse_amount("1,234.56 GBP") == (Decimal("1234.56"), "GBP")
    assert ms._parse_amount("") is None
    assert ms._parse_amount("100") is None  # no currency
    assert ms._parse_amount("abc EUR") is None


# --- _category ------------------------------------------------------------


def test_category_strips_trailing_currency() -> None:
    assert ms._category("Expenses:Pic:K1:Management:EUR") == "Management"
    assert ms._category("Expenses:Pic:K1:Interest:GBP") == "Interest"
    assert ms._category("Expenses:Pic:K1:Brokerage:USD") == "Brokerage"
    # No trailing currency (the ``Other`` leg) → its own leaf.
    assert ms._category("Expenses:Pic:K1:Other") == "Other"


# --- bucketing + GBP conversion -------------------------------------------


def _result() -> QueryResult:
    # The writer suffixes each cost leg with a currency segment
    # (``…:Management:EUR``), so the category is the segment before it.
    return QueryResult(
        rows=[
            # date, account, "<amount> <ccy>"
            ["2024-05-01", "Expenses:Pic:K1:Management:EUR", "1000 EUR"],  # → 850
            ["2024-06-01", "Expenses:Pic:K1:Brokerage:EUR", "100 EUR"],   # → 85 txn
            ["2024-06-01", "Expenses:Pic:K1:Spread:USD", "100 USD"],      # → 75 txn
            ["2024-07-01", "Expenses:Pic:K1:Interest:EUR", "2000 EUR"],   # → 1700
            ["2025-05-01", "Expenses:Pic:K1:Management:EUR", "1000 EUR"],  # → 850 '25
            ["2024-08-01", "Expenses:Pic:K1:Tax:JPY", "40 JPY"],   # no rate → gap
        ]
    )


def test_build_buckets_and_converts() -> None:
    report = ms.build_cost_report(
        _result(), rate_source=FakeRates(), timeline=_timeline()
    )
    years = {c.year: c for c in report.years}
    assert set(years) == {"2024", "2025"}

    y24 = years["2024"]
    assert y24.management_gbp == Decimal("850")
    assert y24.transaction_gbp == Decimal("160")  # 85 + 75
    assert y24.interest_gbp == Decimal("1700")
    assert y24.total_gbp == Decimal("2710")
    # Denominator: average of the two 2024 gross-long points.
    assert y24.avg_invested_gbp == Decimal("1000000")

    y25 = years["2025"]
    assert y25.management_gbp == Decimal("850")
    assert y25.transaction_gbp == Decimal("0")
    assert y25.total_gbp == Decimal("850")
    assert y25.avg_invested_gbp == Decimal("2000000")


def test_unconvertible_posting_is_a_gap_not_a_cost() -> None:
    report = ms.build_cost_report(
        _result(), rate_source=FakeRates(), timeline=None
    )
    # The JPY Tax row had no rate → one gap, excluded from the totals.
    assert len(report.rate_gaps) == 1
    gap = report.rate_gaps[0]
    assert gap.currency == "JPY"
    assert gap.month == "2024-08"
    # No timeline → no denominator.
    assert all(c.avg_invested_gbp is None for c in report.years)


def test_other_legs_excluded_by_query() -> None:
    # ``:Other`` is filtered in the BQL, but a stray one must still bucket as
    # transaction if it ever reached the builder (defensive: unknown leaf).
    assert ms._BUCKET.get("Other", ms._TRANSACTION) == ms._TRANSACTION
    assert 'NOT account ~ ":Other"' in ms._BQL


# --- rendering ------------------------------------------------------------


def test_render_markdown_has_totals_and_share() -> None:
    report = ms.build_cost_report(
        _result(), rate_source=FakeRates(), timeline=_timeline()
    )
    md = ms.render_markdown(report)
    assert "# Mandate cost scorecard" in md
    assert "| **All years** |" in md
    # 2024 total 2,710 / 1,000,000 ≈ 0.3% (pct is 1-dp).
    assert "0.3%" in md
    # The JPY gap is surfaced.
    assert "missing GBP rate" in md
    assert "JPY 2024-08" in md


def test_render_csv_rows_header_and_share() -> None:
    report = ms.build_cost_report(
        _result(), rate_source=FakeRates(), timeline=_timeline()
    )
    rows = ms.render_csv_rows(report)
    assert rows[0] == [
        "year", "management_gbp", "transaction_gbp", "interest_gbp",
        "total_gbp", "avg_invested_gbp", "cost_pct",
    ]
    y24 = next(r for r in rows if r[0] == "2024")
    assert y24[4] == "2710.00"
    assert y24[5] == "1000000.00"
    assert y24[6] == "0.27"  # 2710 / 1000000 * 100


def test_empty_result_renders_cleanly() -> None:
    report = ms.build_cost_report(
        QueryResult(rows=[]), rate_source=FakeRates(), timeline=None
    )
    assert report.years == ()
    md = ms.render_markdown(report)
    assert "| **All years** | £0.00 |" in md
