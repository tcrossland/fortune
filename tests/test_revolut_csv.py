"""Tests for the Revolut CSV → beancount importer.

Synthetic CSVs are written to a tmp_path so the test stays self-contained
and doesn't require real exports. The shape mirrors the Personal app's
Statement → CSV format.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from banking_pipeline.revolut import account_map
from banking_pipeline.revolut.csv_importer import import_csvs, parse_csv
from banking_pipeline.revolut.render import render, render_open_directives

CSV_HEADER = (
    "Type,Product,Started Date,Completed Date,Description,"
    "Amount,Fee,Currency,State,Balance\n"
)


def _write(path: Path, body: str) -> Path:
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


# --- account mapping ------------------------------------------------------


def test_account_map_main_current() -> None:
    assert account_map.asset_account("Current", "GBP") == "Assets:Revolut:Personal:GBP"
    assert account_map.asset_account("Current", "EUR") == "Assets:Revolut:Personal:EUR"


def test_account_map_pro() -> None:
    assert account_map.asset_account("Pro", "EUR") == "Assets:Revolut:Personal:Pro:EUR"


def test_account_map_flexible_cash() -> None:
    assert (
        account_map.asset_account("Savings", "USD")
        == "Assets:Revolut:Personal:FlexibleCash:USD"
    )
    # Alias used in some export vintages — should land in the same place.
    assert (
        account_map.asset_account("Flexible Cash Funds", "GBP")
        == "Assets:Revolut:Personal:FlexibleCash:GBP"
    )


def test_account_map_unknown_product_sanitises() -> None:
    # Unknown products get a sanitised segment so the account is still parseable.
    # Multi-word product names collapse to a single capitalised segment so
    # the account tree stays clean (no internal whitespace, no mixed case).
    assert (
        account_map.asset_account("Crypto Stuff", "BTC")
        == "Assets:Revolut:Personal:Cryptostuff:BTC"
    )


# --- parse: schema tolerance ---------------------------------------------


def test_parse_drops_non_completed(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp.csv",
        "CARD_PAYMENT,Current,2026-04-15 10:00:00,2026-04-15 10:00:00,Tesco,-23.50,0.00,GBP,COMPLETED,500.00\n"
        "CARD_PAYMENT,Current,2026-04-15 11:00:00,,Reverted purchase,-99.00,0.00,GBP,REVERTED,500.00\n"
        "CARD_PAYMENT,Current,2026-04-15 12:00:00,,Pending,-10.00,0.00,GBP,PENDING,490.00\n",
    )
    txns = import_csvs([csv])
    assert len(txns) == 1
    assert txns[0].narration == "Tesco"


def test_parse_missing_required_column_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("Type,Amount\nCARD_PAYMENT,-1.00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        parse_csv(bad)


# --- simple transactions -------------------------------------------------


def test_card_payment_renders_two_postings(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp.csv",
        "CARD_PAYMENT,Current,2026-04-15 10:00:00,2026-04-15 10:00:00,Tesco,-23.50,0.00,GBP,COMPLETED,500.00\n",
    )
    out = render(import_csvs([csv]))
    assert '2026-04-15 * "Tesco" "Tesco"' in out
    assert "Assets:Revolut:Personal:GBP" in out
    assert "-23.50 GBP" in out
    assert account_map.EXPENSES_FIXME in out
    assert "23.50 GBP" in out


def test_fee_splits_counterparty(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp.csv",
        "ATM,Current,2026-04-15 10:00:00,2026-04-15 10:00:00,Cash withdrawal,-100.50,0.50,GBP,COMPLETED,400.00\n",
    )
    out = render(import_csvs([csv]))
    # Asset out: 100.50; counterparty 100.00; fees 0.50.
    assert "-100.50 GBP" in out
    assert "100.00 GBP" in out
    assert account_map.EXPENSES_FEES in out
    assert "0.50 GBP" in out


def test_topup_uses_opening_balances(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp.csv",
        "TOPUP,Current,2026-04-15 10:00:00,2026-04-15 10:00:00,Top-Up by *4242,1000.00,0.00,GBP,COMPLETED,1000.00\n",
    )
    out = render(import_csvs([csv]))
    assert account_map.EQUITY_OPENING in out


def test_interest_uses_income_interest(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp_savings.csv",
        "INTEREST,Savings,2026-04-15 23:59:59,2026-04-15 23:59:59,Interest,1.23,0.00,GBP,COMPLETED,1001.23\n",
    )
    out = render(import_csvs([csv]))
    assert "Assets:Revolut:Personal:FlexibleCash:GBP" in out
    assert account_map.INCOME_INTEREST in out


# --- exchange pairing ----------------------------------------------------


def test_exchange_pair_matches_across_files(tmp_path: Path) -> None:
    gbp = _write(
        tmp_path / "gbp.csv",
        "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to EUR,-100.00,0.00,GBP,COMPLETED,400.00\n",
    )
    eur = _write(
        tmp_path / "eur.csv",
        "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to GBP,117.50,0.00,EUR,COMPLETED,200.00\n",
    )
    txns = import_csvs([gbp, eur])
    # Two simple txns → 0; one paired exchange → 1.
    assert len(txns) == 1
    out = render(txns)
    assert "Exchange GBP → EUR" in out
    assert "-100.00 GBP @@ 117.50 EUR" in out
    assert "Assets:Revolut:Personal:EUR" in out
    assert "Assets:Revolut:Personal:GBP" in out


def test_unmatched_exchange_is_flagged(tmp_path: Path) -> None:
    gbp = _write(
        tmp_path / "gbp.csv",
        "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to EUR,-100.00,0.00,GBP,COMPLETED,400.00\n",
    )
    txns = import_csvs([gbp])
    out = render(txns)
    # Flagged with ! and routed to a placeholder.
    assert '2026-04-15 ! ' in out
    assert account_map.EXPENSES_FIXME in out


def test_exchange_picks_correct_partner_with_two_pairs_same_minute(tmp_path: Path) -> None:
    """If two unrelated exchanges share a Started Date, currency-tagged
    descriptions must still pair correctly."""

    csv = tmp_path / "all.csv"
    csv.write_text(
        CSV_HEADER
        + "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to EUR,-100.00,0.00,GBP,COMPLETED,400.00\n"
        + "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to GBP,117.50,0.00,EUR,COMPLETED,200.00\n"
        + "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to USD,-50.00,0.00,GBP,COMPLETED,350.00\n"
        + "EXCHANGE,Current,2026-04-15 10:30:00,2026-04-15 10:30:00,Exchanged to GBP,62.00,0.00,USD,COMPLETED,62.00\n",
        encoding="utf-8",
    )
    txns = import_csvs([csv])
    # Both exchanges paired → 2 transactions, none flagged.
    assert len(txns) == 2
    assert all(not t.flagged for t in txns)


# --- balance assertions --------------------------------------------------


def test_eod_balance_assertion_emitted(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp.csv",
        "CARD_PAYMENT,Current,2026-04-15 09:00:00,2026-04-15 09:00:00,Tesco,-10.00,0.00,GBP,COMPLETED,490.00\n"
        "CARD_PAYMENT,Current,2026-04-15 11:00:00,2026-04-15 11:00:00,Boots,-5.00,0.00,GBP,COMPLETED,485.00\n",
    )
    out = render(import_csvs([csv]))
    # Asserted at start of 2026-04-16 with the latest balance of the day.
    assert "2026-04-16 balance" in out
    assert "485.00 GBP" in out
    assert "490.00 GBP" not in out  # earlier intra-day balance shouldn't leak through


# --- open directives -----------------------------------------------------


def test_open_directives_cover_all_revolut_accounts(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "all.csv",
        "CARD_PAYMENT,Current,2026-04-15 10:00:00,2026-04-15 10:00:00,Tesco,-1.00,0.00,GBP,COMPLETED,1.00\n"
        "INTEREST,Savings,2026-04-15 23:59:59,2026-04-15 23:59:59,Interest,1.00,0.00,EUR,COMPLETED,1.00\n",
    )
    txns = import_csvs([csv])
    out = render_open_directives(txns)
    assert "open Assets:Revolut:Personal:GBP GBP" in out
    assert "open Assets:Revolut:Personal:FlexibleCash:EUR EUR" in out
    # Non-Revolut placeholders are deliberately not opened by this helper.
    assert "Expenses:FIXME" not in out


# --- determinism ---------------------------------------------------------


def test_output_is_deterministic(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "gbp.csv",
        "CARD_PAYMENT,Current,2026-04-15 10:00:00,2026-04-15 10:00:00,Tesco,-23.50,0.00,GBP,COMPLETED,500.00\n"
        "CARD_PAYMENT,Current,2026-04-15 11:00:00,2026-04-15 11:00:00,Boots,-7.50,0.00,GBP,COMPLETED,492.50\n",
    )
    a = render(import_csvs([csv]))
    b = render(import_csvs([csv]))
    assert a == b


def test_amounts_quantize_to_two_decimals(tmp_path: Path) -> None:
    """Two decimal places, even when the source has more (rare for fiat)."""

    csv = _write(
        tmp_path / "gbp.csv",
        "INTEREST,Savings,2026-04-15 23:59:59,2026-04-15 23:59:59,Interest,1.234,0.00,GBP,COMPLETED,1001.23\n",
    )
    out = render(import_csvs([csv]))
    assert "1.23 GBP" in out
    assert Decimal("1.23") == Decimal("1.23")  # sanity
