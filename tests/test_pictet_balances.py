"""Regression tests for the Pictet monthly-statement balance parser.

Covers two failure modes seen on valuation pages that carry a lombard
credit-limit line and residual zero-balance currency rows. All figures
below are synthetic.
"""

from __future__ import annotations

from banking_pipeline.balances_extract import extract_balances_from_statement

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
