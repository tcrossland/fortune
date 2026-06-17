"""Shared report formatting helpers."""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.report_format import (
    gbp,
    missing_price_lines,
    money,
    pct,
    rate_gap_lines,
    unclassified_lines,
    weight,
)
from banking_pipeline.tax.uk.currency import RateGap

D = Decimal


def test_money_and_gbp_formatting() -> None:
    assert money(D("1234.5")) == "1234.50"
    assert gbp(D("1234.5")) == "£1,234.50"
    assert gbp(D("-1000")) == "£-1,000.00"


def test_pct_and_weight_zero_total() -> None:
    # pct shows an em dash on a zero denominator; weight (CSV) goes blank.
    assert pct(D("5"), D("0")) == "—"
    assert weight(D("5"), D("0")) == ""
    assert pct(D("25"), D("100")) == "25.0%"
    assert weight(D("25"), D("100")) == "25.0"
    # weight is the bare number of pct (no % sign), same rounding.
    assert weight(D("1"), D("3")) == "33.3"
    assert pct(D("1"), D("3")) == "33.3%"


def test_unclassified_lines_empty_and_populated() -> None:
    assert unclassified_lines([]) == []
    out = "\n".join(unclassified_lines(["B", "A", "A"]))
    assert "Unclassified holdings (no metadata)" in out
    assert "commodities.toml" in out
    # Deduped + sorted.
    assert out.count("- A") == 1
    assert out.index("- A") < out.index("- B")


def test_missing_price_lines_empty_and_populated() -> None:
    assert missing_price_lines([]) == []
    out = "\n".join(missing_price_lines(["IE00X"]))
    assert "Unvaluable holdings (no statement mark)" in out
    assert "- IE00X" in out


def test_rate_gap_lines_dedup_and_order() -> None:
    gaps = [
        RateGap("IE00X", "USD", "2025-06"),
        RateGap("IE00X", "USD", "2025-06"),  # dup
        RateGap("IE00Y", "EUR", "2025-05"),
    ]
    out = rate_gap_lines(gaps, title="T", intro="I")
    body = "\n".join(out)
    assert "## ⚠️ T" in body
    assert body.count("EUR 2025-05") == 1
    # Ordered by currency then month: EUR before USD.
    assert body.index("EUR 2025-05") < body.index("USD 2025-06")
