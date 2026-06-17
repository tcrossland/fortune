"""Trial-balance report: parsing, GBP valuation of Assets, rendering.

The bean-query subprocess is not exercised here — `build_trial_balance`
takes a `QueryResult`, so the pure logic (parse, value, render) is tested
with synthetic rows. A `FakeRates` stands in for the GBP rate source.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline import trial_balance as tb_mod
from banking_pipeline.bean_query import QueryResult

ON = date(2026, 6, 17)


class FakeRates:
    """Structural GbpRateSource: fixed EUR/USD rates, nothing else."""

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return {"EUR": Decimal("0.85"), "USD": Decimal("0.75")}.get(
            currency.upper()
        )


# --- parse_amounts --------------------------------------------------------


def test_parse_amounts_variants() -> None:
    assert tb_mod.parse_amounts("") == []
    assert tb_mod.parse_amounts("   ") == []
    assert tb_mod.parse_amounts("100.5 IE00X") == [(Decimal("100.5"), "IE00X")]
    # bean-query comma-joins a multi-commodity cell (plain numbers, no
    # thousands separators) — matching the real CSV format.
    assert tb_mod.parse_amounts(" 202237.67 GBP,   42035.88 EUR") == [
        (Decimal("202237.67"), "GBP"),
        (Decimal("42035.88"), "EUR"),
    ]
    # Zero legs dropped.
    assert tb_mod.parse_amounts("5 GBP, 0 USD, -3 EUR") == [
        (Decimal("5"), "GBP"),
        (Decimal("-3"), "EUR"),
    ]


# --- build ----------------------------------------------------------------


def _result() -> QueryResult:
    return QueryResult(
        rows=[
            # account, units, market
            ["Assets:Pic:K1:IE00X", "100 IE00X", "5000 EUR"],   # sec → 4250
            ["Assets:Pic:K1:GBP", "1000 GBP", "1000 GBP"],      # gbp cash
            ["Assets:Pic:K1:EUR", "2000 EUR", "2000 EUR"],      # eur cash → 1700
            ["Assets:Pic:K1:IE00Y", "50 IE00Y", "50 IE00Y"],    # no mark → missing
            ["Assets:Pic:K1:JPY", "1000 JPY", "1000 JPY"],      # no rate → gap
            ["Income:Pic:K1:Dividend", "-300 EUR", "-300 EUR"], # native only
            ["Assets:Pic:K1:CLOSED", "", ""],                    # dropped
        ]
    )


def test_build_values_assets_and_skips_others() -> None:
    tb = tb_mod.build_trial_balance(
        _result(), on_date=ON, rate_source=FakeRates()
    )
    by_acct = {line.account: line for line in tb.lines}

    # Closed/empty account dropped.
    assert "Assets:Pic:K1:CLOSED" not in by_acct
    # Security valued from its market leg (5000 EUR × 0.85).
    assert by_acct["Assets:Pic:K1:IE00X"].value_gbp == Decimal("4250.00")
    # GBP cash is 1:1; EUR cash converted (2000 × 0.85).
    assert by_acct["Assets:Pic:K1:GBP"].value_gbp == Decimal("1000")
    assert by_acct["Assets:Pic:K1:EUR"].value_gbp == Decimal("1700.00")
    # Income carries no GBP value.
    assert by_acct["Income:Pic:K1:Dividend"].value_gbp is None
    assert by_acct["Income:Pic:K1:Dividend"].native == ((Decimal("-300"), "EUR"),)

    # Total nets only the valued Asset legs (4250 + 1000 + 1700).
    assert tb.assets_gbp == Decimal("6950.00")
    # The unmarked security and the no-rate currency are flagged, not valued.
    assert tb.missing_prices == ("Assets:Pic:K1:IE00Y",)
    assert [g.currency for g in tb.rate_gaps] == ["JPY"]
    assert by_acct["Assets:Pic:K1:IE00Y"].value_gbp is None
    assert by_acct["Assets:Pic:K1:JPY"].value_gbp is None


def test_build_short_rows_ignored() -> None:
    tb = tb_mod.build_trial_balance(
        QueryResult(rows=[["only-two", "1 GBP"]]),
        on_date=ON, rate_source=FakeRates(),
    )
    assert tb.lines == ()


# --- render ---------------------------------------------------------------


def test_render_markdown_columns_and_warnings() -> None:
    tb = tb_mod.build_trial_balance(
        _result(), on_date=ON, rate_source=FakeRates()
    )
    md = "\n".join(tb_mod.render_markdown(tb))

    # Assets section has the 4-column (GBP) header and a GBP subtotal.
    assert "| Account | Commodity | Balance | GBP (mkt) |" in md
    assert "**Assets GBP (market):** £6,950.00" in md
    # Income section is native-only (3 columns).
    assert "## Income (1)" in md
    assert "| Account | Commodity | Balance |\n|---|---|---:|" in md
    # Both warning sections render.
    assert "Unvaluable assets (no mark)" in md
    assert "`Assets:Pic:K1:IE00Y`" in md
    assert "missing rate" in md
    # B11: the rate-gap line names the account, not the useless "(JPY)".
    assert "- JPY 2026-06 (Assets:Pic:K1:JPY)" in md


def test_render_csv_rows() -> None:
    tb = tb_mod.build_trial_balance(
        _result(), on_date=ON, rate_source=FakeRates()
    )
    rows = tb_mod.render_csv_rows(tb)
    assert rows[0] == ["account", "type", "commodity", "balance", "gbp"]
    eur = [r for r in rows if r[0] == "Assets:Pic:K1:EUR"][0]
    assert eur == ["Assets:Pic:K1:EUR", "Assets", "EUR", "2000", "1700.00"]
    # Income row carries no GBP cell.
    inc = [r for r in rows if r[0] == "Income:Pic:K1:Dividend"][0]
    assert inc[4] == ""


def test_multi_leg_account_partially_valued() -> None:
    # An account holding valued cash + an unmarked security: B7 keeps the
    # cash in the GBP total instead of dropping the whole account, and still
    # flags the unmarked leg.
    result = QueryResult(
        rows=[[
            "Assets:Pic:K1:MIXED",
            "1000 GBP, 50 IE00NOMARK",          # units (display)
            "1000 GBP, 50 IE00NOMARK",          # market (one leg unmarked)
        ]]
    )
    tb = tb_mod.build_trial_balance(result, on_date=ON, rate_source=FakeRates())
    line = tb.lines[0]
    # The GBP leg is valued; the account is still flagged for the unmarked leg.
    assert line.value_gbp == Decimal("1000")
    assert tb.assets_gbp == Decimal("1000")
    assert tb.missing_prices == ("Assets:Pic:K1:MIXED",)


def test_rate_gap_carries_account_not_currency() -> None:
    # B11: a no-rate currency leg flags the account in the RateGap slot.
    result = QueryResult(
        rows=[["Assets:Pic:K1:JPY", "1000 JPY", "1000 JPY"]]
    )
    tb = tb_mod.build_trial_balance(result, on_date=ON, rate_source=FakeRates())
    assert [g.isin for g in tb.rate_gaps] == ["Assets:Pic:K1:JPY"]
    assert [g.currency for g in tb.rate_gaps] == ["JPY"]
