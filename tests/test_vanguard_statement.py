"""Balance + price extraction from Vanguard ISA regular statements."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline import balances_extract, prices_extract
from banking_pipeline.models import DocumentType
from banking_pipeline.vanguard_statement import (
    IsaClosure,
    parse_isa_nil_statement,
    parse_isa_valuation,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "en" / "vanguard_uk"
_OPENING = (_FIXTURES / "vanguard_regular_statement.txt").read_text(encoding="utf-8")
_CLOSURE = (
    _FIXTURES / "vanguard_regular_statement.closure.txt"
).read_text(encoding="utf-8")

# A mid-life statement where both funds were traded out within the
# period: Vanguard prints both movement legs so the snapshot nets to
# zero, and only the cash row carries a balance. Mirrors the real
# 12-Aug-2025 statement layout.
_MOVEMENT_PAIRS = """\
Account number: VG0000000
Your ISA investments at 12 August 2025
Description Quantity Price Value
FTSE 250 UCITS ETF - Accumulating (VMIG) -13.00 £39.84 -£517.99
FTSE 250 UCITS ETF - Accumulating (VMIG) 13.00 £39.84 £517.99
U.K. Gilt UCITS ETF - Accumulating -25.00 £20.07 -£501.69
U.K. Gilt UCITS ETF - Accumulating 25.00 £20.07 £501.69
Cash account - - £1,040.53
Activity from 13 May 2025 to 12 August 2025 for your ISA
"""


# --- parse_isa_valuation ---------------------------------------------------


def test_parse_opening_snapshot() -> None:
    v = parse_isa_valuation(_OPENING)
    assert v is not None
    assert v.statement_date.isoformat() == "2025-05-12"
    assert v.account_number == "VG0000000"  # scrubbed fixture
    assert v.cash_balance == Decimal("17.00")
    assert {(h.ticker, h.quantity, h.price) for h in v.holdings} == {
        ("VMIG", Decimal("13.00"), Decimal("37.41")),
        ("VGVA", Decimal("25.00"), Decimal("19.92")),
    }


def test_movement_pairs_net_to_zero_and_resolve_missing_ticker() -> None:
    v = parse_isa_valuation(_MOVEMENT_PAIRS)
    assert v is not None
    assert v.cash_balance == Decimal("1040.53")
    qty = {h.ticker: h.quantity for h in v.holdings}
    # VMIG carries its ticker; the gilt rows omit it and resolve by name.
    assert qty == {"VMIG": Decimal("0.00"), "VGVA": Decimal("0.00")}


def test_closure_statement_has_no_valuation_rows() -> None:
    v = parse_isa_valuation(_CLOSURE)
    # The section exists but is empty (all sold, no cash row).
    assert v is not None
    assert v.holdings == ()
    assert v.cash_balance is None


def test_no_valuation_section_returns_none() -> None:
    assert parse_isa_valuation("Some unrelated document text.") is None


# --- parse_isa_nil_statement (drained wind-down detection) ------------------


def test_nil_statement_detected_on_zero_current_account_total() -> None:
    # The closure fixture's current-column ``Account total`` is £0.00.
    assert parse_isa_nil_statement(_CLOSURE) == IsaClosure(
        statement_date=date(2026, 2, 12), account_number="VG0000000"
    )


def test_nil_statement_none_when_current_total_nonzero() -> None:
    # The funded opening statement must not read as a wind-down — its
    # current-column total is non-zero, so a live account is never zeroed.
    assert parse_isa_nil_statement(_OPENING) is None


def test_nil_statement_none_on_unrelated_document() -> None:
    assert parse_isa_nil_statement("Some unrelated document text.") is None


def test_nil_statement_none_when_only_prior_column_is_zero() -> None:
    # Prior column £0.00 but current £5.00 → funded, not a wind-down. Guards
    # against matching the wrong (left) column.
    text = (
        "Account number: VG0000000\n"
        "Product Value on 13 November 2025 Value on 12 February 2026\n"
        "Account total £0.00 £5.00\n"
    )
    assert parse_isa_nil_statement(text) is None


# --- balances_extract ------------------------------------------------------


def test_balances_opening_emits_cash_and_nonzero_holdings() -> None:
    rows = set(balances_extract.extract_balances_from_statement(_OPENING))
    assert rows == {
        ("2025-05-13", "Assets:Vgd:ISA:VG0000000:GBP", "17.00", "GBP"),
        ("2025-05-13", "Assets:Vgd:ISA:VG0000000:VGVA", "25.00", "VGVA"),
        ("2025-05-13", "Assets:Vgd:ISA:VG0000000:VMIG", "13.00", "VMIG"),
    }


def test_balances_skip_zero_net_holdings() -> None:
    rows = balances_extract.extract_balances_from_statement(_MOVEMENT_PAIRS)
    # Only the cash assertion — the wound-down positions net to zero.
    assert rows == [
        ("2025-08-13", "Assets:Vgd:ISA:VG0000000:GBP", "1040.53", "GBP"),
    ]


def test_balances_empty_on_closure() -> None:
    assert balances_extract.extract_balances_from_statement(_CLOSURE) == []


# --- prices_extract --------------------------------------------------------


def test_prices_from_opening_statement() -> None:
    rows = prices_extract.extract_prices_from_statement(
        _OPENING,
        doctype=DocumentType.VANGUARD_REGULAR_STATEMENT,
        source="s.pdf",
    )
    assert {(r.date, r.commodity, r.price, r.currency) for r in rows} == {
        ("2025-05-12", "VMIG", "37.41", "GBP"),
        ("2025-05-12", "VGVA", "19.92", "GBP"),
    }


def test_prices_skipped_for_non_priced_doctype() -> None:
    rows = prices_extract.extract_prices_from_statement(
        _OPENING,
        doctype=DocumentType.VANGUARD_ISA_DECLARATION,
        source="s.pdf",
    )
    assert rows == []


def test_trade_derived_prices_capture_ticker_commodities() -> None:
    """The broadened posting regex must pick up short ticker commodities
    (Vanguard) as well as 12-char ISINs (Pictet) from the ledger's
    cost-basis / market-price annotations."""

    rows = prices_extract.extract_prices(
        [_FIXTURES / "vanguard_contract_note_buy.beancount"]
    )
    assert {(r.commodity, r.price) for r in rows} == {
        ("VMIG", "37.330600"),
        ("VGVA", "19.918060"),
    }
