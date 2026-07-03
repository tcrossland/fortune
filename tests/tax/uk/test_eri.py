"""Excess reportable income (ERI) computation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from banking_pipeline.fx.gbp_rates import HmrcMonthlyAverageSource
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.tax.uk.eri import (
    EriEntry,
    compute_eri,
    cumulative_base_cost_adjustments,
    load_eri,
)


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


def test_cumulative_adjustments_merge_across_tax_years() -> None:
    # The pool is cumulative, so a current cost basis needs every year's ERI
    # uplift — not one year's, which is all a single compute_eri returns.
    isin = "IE00B3VWN518"
    txs = [_buy(isin, "1000", date(2023, 1, 10))]
    entries = {
        isin: [
            _entry(isin, period_end=date(2023, 6, 30),
                   fund_distribution_date=date(2023, 12, 30)),  # 2023-24
            _entry(isin, period_end=date(2024, 6, 30),
                   fund_distribution_date=date(2024, 12, 30)),  # 2024-25
        ]
    }
    adjustments, gaps = cumulative_base_cost_adjustments(
        txs, eri_entries=entries, commodities={}, source=None,
    )
    assert gaps == []
    # One adjustment per tax year, merged into a single per-ISIN list.
    assert len(adjustments[isin]) == 2
    # Each: units × (eri − equalisation) = 1000 × (0.50 − 0.10) = 400.
    assert sum((a.cost_gbp for a in adjustments[isin]), Decimal(0)) == Decimal("800")

    # A single-year call sees only that year's entry — the gap this closes.
    single = compute_eri(
        txs, tax_year_label="2023-24", eri_entries=entries, commodities={},
    )
    assert len(single.base_cost_adjustments[isin]) == 1


def test_measurement_date_derived_from_distribution() -> None:
    # period_end unset → six months (month end) before the distribution.
    entry = EriEntry(
        isin="IE00B3VWN518",
        fund_distribution_date=date(2025, 3, 31),
        income_type="interest",
        eri_per_unit=Decimal("0.5"),
        currency="GBP",
    )
    assert entry.period_end is None
    assert entry.measurement_date == date(2024, 9, 30)


def test_measurement_date_explicit_period_end_overrides() -> None:
    entry = _entry("IE00B3VWN518", period_end=date(2024, 6, 30))
    assert entry.measurement_date == date(2024, 6, 30)


def test_units_measured_six_months_before_distribution() -> None:
    # Held at the derived period end (30 Sep 2024) but sold before the
    # distribution date — Pictet's convention still reports the income.
    isin = "IE00B3VWN518"
    txs = [
        _buy(isin, "1000", date(2024, 1, 1)),
        Transaction(
            trade_date=date(2024, 11, 1),
            narration="sell",
            currency="GBP",
            amount=Decimal("1500"),
            isin=isin,
            quantity=Decimal("-1000"),
            document_type=DocumentType.SELL_ETF,
            source_path=Path("t.pdf"),
        ),
    ]
    entry = EriEntry(
        isin=isin,
        fund_distribution_date=date(2025, 3, 31),
        income_type="interest",
        eri_per_unit=Decimal("0.50"),
        currency="GBP",
    )
    result = compute_eri(
        txs, tax_year_label="2024-25", eri_entries={isin: [entry]}, commodities={}
    )
    assert len(result.rows) == 1
    assert result.rows[0].gross_gbp == Decimal("500.00")  # 1000 units * 0.50


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
    assert row.gross_gbp == Decimal("500.00")  # 1000 * 0.50, taxable income
    assert row.equalisation_gbp == Decimal("100.00")  # 1000 * 0.10
    # Equalisation is not netted off income, only off the base cost.
    assert row.base_cost_adjustment_gbp == Decimal("400.00")
    # Base-cost uplift = gross - equalisation, at the distribution date.
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
    # The gap carries the currency + the distribution month (2024-12).
    assert len(result.missing_rates) == 1
    gap = result.missing_rates[0]
    assert (gap.isin, gap.currency, gap.month) == (isin, "EUR", "2024-12")


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
    # base cost uplift = gross - equalisation = (500 - 100) EUR * 0.80
    assert result.rows[0].base_cost_adjustment_gbp == Decimal("320.00")


def test_bond_fund_eri_overridden_to_interest_and_flagged() -> None:
    # A bond fund (distributions_as_interest) whose eri.toml entry is wrongly
    # typed "dividend" — ERI must follow the commodity flag (interest), and
    # the inconsistency is flagged for the user to fix the TOML.
    from banking_pipeline.commodities_metadata import CommodityMetadata

    isin = "IE00B3VWN518"
    meta = CommodityMetadata(
        isin=isin, name="Bond Fund", domicile="IE",
        reporting_status="reporting", asset_class="bond",
        first_acquired=date(2020, 1, 1), distributions_as_interest=True,
    )
    result = compute_eri(
        [_buy(isin, "1000", date(2024, 1, 1))],
        tax_year_label="2024-25",
        eri_entries={isin: [_entry(isin, income_type="dividend")]},
        commodities={isin: meta},
    )
    assert [r.income_type for r in result.rows] == ["interest"]
    assert result.reclassified_to_interest == [isin]


def test_eri_income_type_respected_without_bond_fund_flag() -> None:
    # No bond-fund flag → the typed income_type stands, nothing flagged.
    isin = "IE00B3VWN518"
    result = compute_eri(
        [_buy(isin, "1000", date(2024, 1, 1))],
        tax_year_label="2024-25",
        eri_entries={isin: [_entry(isin, income_type="dividend")]},
        commodities={},
    )
    assert [r.income_type for r in result.rows] == ["dividend"]
    assert result.reclassified_to_interest == []
