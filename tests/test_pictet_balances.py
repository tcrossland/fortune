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
from banking_pipeline.commodities_metadata import normalise_security_name

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


# --- Pictet P mandate "Financial Statement" by-name layout ---------------
# Holdings are printed by an abbreviated name with NO ``ISIN:`` marker, and
# cash rows carry the GBP-reference conversion column + a weight. All
# figures / names below are synthetic. The credit-limit row and a
# zero-balance currency exercise the same rejections as the K layout.
_P_BYNAME_STATEMENT = """\
As at 30 June 2025
Account no.: P-123456.002

Cash GBP -100'000.00 271.42%
Current accounts GBP -100'000.00 271.42%
-30'000.00 Pound United Kingdom GBP -30'000.00 GBP -30'000.00 113.55%
-54'000.00 Dollar USA USD -54'000.00 GBP -40'000.00 79.43%
0.00 Euro EUR 0.00 GBP 0.00 0.00%
Equities GBP 50'000.00 -132.94%
1'365 Acme Defense Etf A Usd USD 39.37 USD 53'740.05 GBP 42'679.63 -13.68%
420 Widget Holdings 'B' DKK 644.50 DKK 270'690.00 GBP 29'979.27 -4.38%
2'400'000.00 C/A Limit Gbp, 26.02.2025-26.02.2026 - Bp Level GBP 0.00 GBP 0.00 0.00%
"""

# Resolves only the first holding — "Widget Holdings 'B'" is left
# deliberately unmapped to exercise the unresolved-holding guard.
_P_NAME_TO_ISIN = {normalise_security_name("Acme Defense Etf A Usd"): "XX0000000001"}


def test_p_byname_cash_rows_assert_with_gbp_conversion_column() -> None:
    """The P by-name cash row carries a second ``<CCY> <val> <%>`` group the
    K layout collapses away; the origin-currency balance (not the GBP
    conversion) is asserted, and a zero-balance currency is skipped."""

    rows = extract_balances_from_statement(_P_BYNAME_STATEMENT, _P_NAME_TO_ISIN)
    accounts = {r[1]: r for r in rows}
    assert accounts["Assets:Pic:P123456002:GBP"] == (
        "2025-07-01", "Assets:Pic:P123456002:GBP", "-30000.00", "GBP",
    )
    assert accounts["Assets:Pic:P123456002:USD"] == (
        "2025-07-01", "Assets:Pic:P123456002:USD", "-54000.00", "USD",
    )
    assert "Assets:Pic:P123456002:EUR" not in accounts  # zero → skipped


def test_p_byname_cash_asserts_booked_quantity_not_accrued_valuation() -> None:
    """Mid-quarter, the ``Valuation (Orig.)`` column adds accrued-but-unpaid
    interest, so it diverges from the leading booked balance. The assertion
    must use the booked quantity (what the ledger holds), and the row must
    NOT be dropped as a symmetry mismatch — that silently lost a whole
    month's P cash, invisibly to the coverage guard too."""

    text = (
        "As at 30 April 2025\n"
        "Account no.: P-123456.002\n\n"
        # qty -269'090.40 (booked) != orig valuation -269'977.91 (accrued).
        "-269'090.40 Pound United Kingdom GBP -269'977.91 GBP -269'977.91 134.95%\n"
    )
    rows = extract_balances_from_statement(text, _P_NAME_TO_ISIN)
    assert (
        "2025-05-01",
        "Assets:Pic:P123456002:GBP",
        "-269090.40",
        "GBP",
    ) in rows
    # And the coverage guard must not flag it as a dropped cash row.
    assert statement_coverage_gaps(text, _P_NAME_TO_ISIN) == []


def test_loose_p_cash_detector_matches_expanded_format() -> None:
    """The coverage guard's loose P-cash re-detector recognises the expanded
    two-value-column format (balance + currency), so a future tightening of
    the strict parser that dropped a P cash row would surface as a gap
    rather than a silent shortfall — the K/ES detectors can't see this shape
    (it has no doubled-balance symmetry)."""

    from banking_pipeline.balances_extract import _LOOSE_FS_CASH_RE

    m = _LOOSE_FS_CASH_RE.match(
        "-90'814.18 Krone Denmark DKK -91'239.59 GBP -10'404.23 5.20%"
    )
    assert m is not None
    assert m.group(1) == "-90'814.18"
    assert m.group(2) == "DKK"
    # It must not match a three-currency-token security row.
    assert (
        _LOOSE_FS_CASH_RE.match(
            "700 Fujifilm Holdings JPY 3'142.00 JPY 2'199'400 GBP 11'111.44 -4.38%"
        )
        is None
    )


def test_p_byname_credit_limit_row_is_not_asserted() -> None:
    """The ``C/A Limit`` credit-limit row is cash-shaped but its quantity
    (2'400'000) doesn't repeat as the valuation (0.00); it must not assert a
    bogus GBP balance (the GBP assertion stays the -30'000 cash line)."""

    rows = extract_balances_from_statement(_P_BYNAME_STATEMENT, _P_NAME_TO_ISIN)
    gbp = [r for r in rows if r[1] == "Assets:Pic:P123456002:GBP"]
    assert gbp == [
        ("2025-07-01", "Assets:Pic:P123456002:GBP", "-30000.00", "GBP")
    ]


def test_p_byname_security_resolves_name_to_isin() -> None:
    """A by-name holding whose display name resolves to a ledger ISIN is
    asserted by quantity against that ISIN's sub-account."""

    rows = extract_balances_from_statement(_P_BYNAME_STATEMENT, _P_NAME_TO_ISIN)
    assert (
        "2025-07-01",
        "Assets:Pic:P123456002:XX0000000001",
        "1365",
        "XX0000000001",
    ) in rows


def test_p_byname_unresolved_holding_is_flagged_not_dropped() -> None:
    """A by-name holding with no name→ISIN mapping emits no assertion (it
    can't be keyed to a ledger account) but is reported by the coverage
    guard, so the missing ``statement_names`` alias is visible."""

    rows = extract_balances_from_statement(_P_BYNAME_STATEMENT, _P_NAME_TO_ISIN)
    # No assertion for the unmapped holding.
    assert all("Widget" not in r[1] for r in rows)
    gaps = statement_coverage_gaps(_P_BYNAME_STATEMENT, _P_NAME_TO_ISIN)
    assert ("unresolved-holding", "Widget Holdings 'B'") in [
        (g.kind, g.detail) for g in gaps
    ]


def test_p_byname_fully_mapped_statement_has_no_gaps() -> None:
    """With every by-name holding mapped, the coverage guard is clean."""

    full = dict(_P_NAME_TO_ISIN)
    full[normalise_security_name("Widget Holdings 'B'")] = "XX0000000002"
    assert statement_coverage_gaps(_P_BYNAME_STATEMENT, full) == []


# In the opening months a leveraged base pushes weights off-scale and
# Pictet clamps them to ``> 999.99%`` / ``< -999.99%`` (a comparison
# operator + space). A weight matcher that only accepted a bare number
# dropped every row on the opening statement.
_P_CLAMPED_WEIGHT_STATEMENT = """\
As at 28 February 2025
Account no.: P-123456.002

-30'000.00 Pound United Kingdom GBP -30'000.00 GBP -30'000.00 > 999.99%
1'365 Acme Defense Etf A Usd USD 39.37 USD 53'740.05 GBP 42'679.63 < -999.99%
"""


def test_p_byname_clamped_weight_rows_are_parsed() -> None:
    """Rows whose weight is clamped to ``> 999.99%`` / ``< -999.99%`` still
    parse — the opening-statement drop the empty-statement guard caught."""

    rows = extract_balances_from_statement(
        _P_CLAMPED_WEIGHT_STATEMENT, _P_NAME_TO_ISIN
    )
    accounts = {r[1] for r in rows}
    assert "Assets:Pic:P123456002:GBP" in accounts
    assert "Assets:Pic:P123456002:XX0000000001" in accounts


def test_nonzero_total_statement_with_zero_rows_is_flagged_empty() -> None:
    """A Pictet valuation with a non-zero portfolio total that extracts
    nothing is a whole-statement drop — reported as ``empty-statement``
    (the P by-name layout did exactly this before it had a parser path)."""

    text = (
        "As at 30 June 2025\n"
        "Account no.: P-123456.002\n\n"
        "Total portfolio (including accrued interest GBP 0.00) GBP -253'442.53 100.00%\n"
        "(rows in a layout the parser can't read)\n"
    )
    gaps = statement_coverage_gaps(text)
    assert [g.kind for g in gaps] == ["empty-statement"]


def test_zero_total_opening_statement_is_not_flagged() -> None:
    """A freshly-opened / drained account reports a zero portfolio total and
    genuinely has no rows — it must NOT be flagged as a whole-statement
    drop, or every opening statement fails ``--strict``."""

    text = (
        "al 30 Junio 2021\n"
        "K-123456.001\n\n"
        "Total de la cartera EUR 0\n"
    )
    assert statement_coverage_gaps(text) == []


def test_unrecognised_document_reports_no_gaps() -> None:
    """A document the parser doesn't recognise as a valuation (no parseable
    header) has nothing to reconcile against — no empty-statement noise."""

    assert statement_coverage_gaps("just some unrelated text\n") == []
