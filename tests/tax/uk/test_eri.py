"""Excess reportable income (ERI) computation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from banking_pipeline.fx.gbp_rates import HmrcMonthlyAverageSource
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.tax.uk.eri import EriEntry, compute_eri, load_eri


def _buy(isin: str, qty: str, on: date) -> Transaction:
    return Transaction(
        trade_date=on,
        narration="buy",
        currency="GBP",
        amount=Decimal("-1000"),
        isin=isin,
        quantity=Decimal(qty),
        document_type=DocumentType.BUY_ETF,
        source_path=Path("t.pdf"),
    )


def _entry(isin: str, **kw: object) -> EriEntry:
    base: dict[str, object] = dict(
        isin=isin,
        period_end=date(2024, 6, 30),
        fund_distribution_date=date(2024, 12, 30),
        income_type="interest",
        eri_per_unit=Decimal("0.50"),
        equalisation_per_unit=Decimal("0.10"),
        currency="GBP",
    )
    base.update(kw)
    return EriEntry(**base)  # type: ignore[arg-type]


def test_load_eri_groups_by_isin(tmp_path: Path) -> None:
    toml = """
[[eri]]
isin = "LU0767911984"
period_end = 2024-06-30
fund_distribution_date = 2024-12-30
income_type = "interest"
eri_per_unit = 0.4521
currency = "EUR"
"""
    path = tmp_path / "eri.toml"
    path.write_text(toml, encoding="utf-8")
    entries = load_eri(path)
    assert list(entries) == ["LU0767911984"]
    assert entries["LU0767911984"][0].income_type == "interest"
    assert entries["LU0767911984"][0].equalisation_per_unit == Decimal(0)


def test_load_eri_rejects_bad_isin(tmp_path: Path) -> None:
    toml = (
        '[[eri]]\nisin = "FOO"\nperiod_end = 2024-06-30\n'
        'fund_distribution_date = 2024-12-30\nincome_type = "interest"\n'
        "eri_per_unit = 0.1\ncurrency = \"EUR\"\n"
    )
    path = tmp_path / "eri.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ValidationError, match="not a valid ISIN"):
        load_eri(path)


def test_income_split_and_equalisation() -> None:
    isin = "IE00B3VWN518"
    # Held 1000 units at period end (bought before it).
    txs = [_buy(isin, "1000", date(2024, 1, 1))]
    result = compute_eri(
        txs,
        tax_year_label="2024-25",
        eri_entries={isin: [_entry(isin)]},
        commodities={},
    )
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.income_type == "interest"
    assert row.gross_gbp == Decimal("500.00")  # 1000 * 0.50
    assert row.equalisation_gbp == Decimal("100.00")  # 1000 * 0.10
    assert row.net_gbp == Decimal("400.00")  # taxable
    # Base-cost uplift = net taxable, at the distribution date.
    adj = result.base_cost_adjustments[isin]
    assert len(adj) == 1
    assert adj[0].date == date(2024, 12, 30)
    assert adj[0].cost_gbp == Decimal("400.00")


def test_no_units_at_period_end_skipped() -> None:
    isin = "IE00B3VWN518"
    # Bought only after the period end → nothing held at period end.
    txs = [_buy(isin, "1000", date(2024, 9, 1))]
    result = compute_eri(
        txs,
        tax_year_label="2024-25",
        eri_entries={isin: [_entry(isin)]},
        commodities={},
    )
    assert result.rows == []
    assert result.base_cost_adjustments == {}


def test_distribution_date_outside_year_excluded() -> None:
    isin = "IE00B3VWN518"
    txs = [_buy(isin, "1000", date(2024, 1, 1))]
    entry = _entry(isin, fund_distribution_date=date(2025, 12, 30))
    result = compute_eri(
        txs, tax_year_label="2024-25", eri_entries={isin: [entry]}, commodities={}
    )
    assert result.rows == []


def test_missing_rate_flags_isin() -> None:
    isin = "IE00B3VWN518"
    txs = [_buy(isin, "1000", date(2024, 1, 1))]
    entry = _entry(isin, currency="EUR")  # no source → no rate
    result = compute_eri(
        txs, tax_year_label="2024-25", eri_entries={isin: [entry]}, commodities={}
    )
    assert result.rows == []
    assert result.missing_rate_isins == [isin]


def test_foreign_currency_uses_rate_source() -> None:
    isin = "IE00B3VWN518"
    txs = [_buy(isin, "1000", date(2024, 1, 1))]
    entry = _entry(isin, currency="EUR")
    source = HmrcMonthlyAverageSource.from_text("month,currency,rate\n2024-12,EUR,0.80\n")
    result = compute_eri(
        txs, tax_year_label="2024-25", eri_entries={isin: [entry]},
        commodities={}, source=source,
    )
    assert result.rows[0].gross_gbp == Decimal("400.00")  # 500 EUR * 0.80
    assert result.rows[0].net_gbp == Decimal("320.00")  # 400 EUR * 0.80
