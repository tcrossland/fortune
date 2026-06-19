"""Regression tests for the Pictet monthly-statement balance parser.

Covers two failure modes seen on valuation pages that carry a lombard
credit-limit line and residual zero-balance currency rows. All figures
below are synthetic.
"""

from __future__ import annotations

from banking_pipeline.balances_extract import (
    extract_balances_from_statement,
    statement_coverage_gaps,
)

# A minimal Pictet valuation page reproducing the problem layout: a
# quantity-led ``C/A Limit`` credit-limit line immediately above a
# holding, a residual zero-balance EUR cash row, and a non-zero DKK cash
# row. Values are invented purely to exercise the parser.
_STATEMENT = """\
As at 31 March 2025
Account no.: P-123456.002

12345.67 Krone Denmark DKK 12345.67
0.00 Euro EUR 0.00
1'000'000.00 C/A Limit Gbp, 26.02.2025-26.02.2026 - Bp Level GBP 0.00 GBP 0.00 0.00%
Miscellaneous GBP 5'000.00 -10.00%
100 ACME WIDGETS PLC USD 50.00 USD 5'000.00 GBP 4'000.00 -10.00%
ISIN/Internal ref.: XX0000000000 Telekurs ID/Internal ref.: 00000000
"""


def test_credit_limit_line_does_not_annex_the_holding_isin() -> None:
    """The quantity-led ``C/A Limit`` credit-limit row sits two lines above
    the holding's ISIN. It must not claim that ISIN: the asserted quantity
    has to be the holding's 100, not the credit-limit figure."""

    rows = extract_balances_from_statement(_STATEMENT)
    held = [r for r in rows if r[1].endswith(":XX0000000000")]
    assert held == [
        ("2025-04-01", "Assets:Pic:P123456002:XX0000000000", "100", "XX0000000000"),
    ]


def test_zero_balance_currency_row_is_skipped() -> None:
    """A residual ``0.00`` cash row would assert against a currency
    sub-account the ledger never opens (bean-check "inactive account"), so
    it's dropped; a non-zero cash row is still asserted."""

    rows = extract_balances_from_statement(_STATEMENT)
    accounts = {r[1] for r in rows}
    assert "Assets:Pic:P123456002:EUR" not in accounts  # zero → skipped
    assert (
        "2025-04-01",
        "Assets:Pic:P123456002:DKK",
        "12345.67",
        "DKK",
    ) in rows


# The Spanish ``ESTADO FINANCIERO`` localises the currency name (``Dólar
# USA``) and rounds the portfolio-currency display column to whole units
# (``522'026``) while the balance column keeps cents (``522'025.77``).
# Both used to drop the row — the accented ``ó`` failed the name class,
# and the rounded column failed the exact-equality symmetry guard.
_ES_CASH_STATEMENT = """\
al 31 Enero 2023
K-123456.001

522'026 Euro EUR 522'025.77
1'039'817.4 Dólar USA USD 1'039'817.40
"""


def test_accented_currency_name_cash_row_is_parsed() -> None:
    """A cash row whose currency name carries an accent (``Dólar USA``)
    must still be captured — an ASCII-only name class silently dropped the
    USD cash leg and understated net cash."""

    rows = extract_balances_from_statement(_ES_CASH_STATEMENT)
    assert (
        "2023-02-01",
        "Assets:Pic:K123456001:USD",
        "1039817.40",
        "USD",
    ) in rows


def test_whole_unit_rounded_display_column_does_not_drop_cash() -> None:
    """When the display column rounds to whole units (``522'026``) but the
    balance column keeps cents (``522'025.77``), the row is still kept and
    the cent-precise balance is asserted."""

    rows = extract_balances_from_statement(_ES_CASH_STATEMENT)
    assert (
        "2023-02-01",
        "Assets:Pic:K123456001:EUR",
        "522025.77",
        "EUR",
    ) in rows


# The newer statement layout concatenates the quantity row and the ISIN
# marker onto one line, joined by a stray control char (here ``￾``).
# The forward-scanning parser only looked at *following* lines, so the
# holding was silently dropped from the valuation.
_CONCAT_ISIN_STATEMENT = (
    "As at 31 March 2026\n"
    "Account no.: K-123456.001\n\n"
    "1'743.00 Eleva-European Selection R Eur-Acc￾ISIN: LU1111643711\n"
    "EUR 255.25 EUR 260.26\n"
    "GBP 396'345.13\n"
)


def test_quantity_and_isin_on_one_line_is_parsed() -> None:
    """A holding whose quantity and ``ISIN:`` marker share a line (joined
    by a control char) must still be extracted, not dropped."""

    rows = extract_balances_from_statement(_CONCAT_ISIN_STATEMENT)
    assert (
        "2026-04-01",
        "Assets:Pic:K123456001:LU1111643711",
        "1743.00",
        "LU1111643711",
    ) in rows


def test_coverage_guard_clean_when_everything_extracted() -> None:
    """A statement whose holdings and cash are all captured reports no
    coverage gaps."""

    assert statement_coverage_gaps(_CONCAT_ISIN_STATEMENT) == []
    assert statement_coverage_gaps(_ES_CASH_STATEMENT) == []


def test_coverage_guard_flags_an_uncaptured_holding() -> None:
    """A holding whose ISIN the parser fails to extract (here a stray
    marker with no quantity row to anchor it) is surfaced as a coverage
    gap — the regression signal that caught the concatenated-ISIN bug."""

    text = (
        "As at 31 March 2026\n"
        "Account no.: K-123456.001\n\n"
        "1'743.00 Eleva-European Selection R Eur-Acc￾ISIN: LU1111643711\n"
        "EUR 255.25 EUR 260.26\n"
        "GBP 396'345.13\n"
        # A second ISIN with no quantity row before it: the parser can't
        # anchor it, so it never gets extracted, but the guard still sees it.
        "ISIN: LU9999999999\n"
    )
    gaps = statement_coverage_gaps(text)
    assert [(g.kind, g.detail) for g in gaps] == [("security", "LU9999999999")]
