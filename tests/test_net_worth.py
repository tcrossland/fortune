"""Net-worth-over-time timeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.fx.gbp_rates import HmrcMonthlyAverageSource, NullSource
from banking_pipeline.net_worth import (
    _timeline_from_raw,
    build_timeline,
    render_markdown,
)
from banking_pipeline.valuation import RawHolding, value_holdings

D = Decimal

_VANGUARD = Path("tests/fixtures/en/vanguard_uk/vanguard_regular_statement.txt")


def _sec(portfolio: str, on: date, key: str, qty: Decimal, price: Decimal) -> RawHolding:
    return RawHolding(portfolio, on, key, qty, price, "GBP", False)


def test_as_of_forward_fill_across_portfolios() -> None:
    """At each date, every portfolio contributes its latest snapshot on or
    before that date — so a portfolio with no fresh statement is carried."""

    raws = [
        _sec("A", date(2025, 1, 1), "IE00B3VWN518", D(10), D(100)),  # A=1000
        _sec("B", date(2025, 2, 1), "LU1287023185", D(5), D(100)),   # B=500
        _sec("A", date(2025, 3, 1), "IE00B3VWN518", D(12), D(100)),  # A=1200
    ]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    pts = {p.on_date: p for p in tl.points}
    assert [p.on_date for p in tl.points] == [
        date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)
    ]
    assert pts[date(2025, 1, 1)].net_worth_gbp == D(1000)
    assert pts[date(2025, 1, 1)].portfolios == 1
    assert pts[date(2025, 1, 1)].change_gbp is None
    # B's first statement appears; A forward-filled at 1000.
    assert pts[date(2025, 2, 1)].net_worth_gbp == D(1500)
    assert pts[date(2025, 2, 1)].portfolios == 2
    assert pts[date(2025, 2, 1)].change_gbp == D(500)
    # A updates to 1200; B forward-filled at 500.
    assert pts[date(2025, 3, 1)].net_worth_gbp == D(1700)
    assert pts[date(2025, 3, 1)].change_gbp == D(200)


def test_duplicate_statements_same_date_not_double_counted() -> None:
    """Two statements covering the same portfolio + as-of date (e.g. a
    monthly and an annual) must not double the holding's value."""

    raws = [
        _sec("A", date(2025, 1, 1), "IE00B3VWN518", D(10), D(100)),
        _sec("A", date(2025, 1, 1), "IE00B3VWN518", D(10), D(100)),  # dup
    ]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    assert len(tl.points) == 1
    assert tl.points[0].net_worth_gbp == D(1000)  # not 2000


def test_leverage_shows_in_net_cash() -> None:
    raws = [
        _sec("A", date(2025, 1, 1), "IE00B3VWN518", D(100), D(100)),  # 10000
        RawHolding("A", date(2025, 1, 1), "GBP", D(-6000), None, "GBP", True),
    ]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    p = tl.points[0]
    assert p.gross_long_gbp == D(10000)
    assert p.net_cash_gbp == D(-6000)
    assert p.net_worth_gbp == D(4000)


def test_month_end_snapshot_marks_to_latest_rate_not_gapped() -> None:
    """A month-end statement dated to the 1st of the next (unpublished)
    month values its non-GBP holdings at the latest known rate instead of
    collapsing to a rate gap — the forward-fill in value_holdings."""

    rates = HmrcMonthlyAverageSource.from_text(
        "month,currency,rate\n2026-06,EUR,0.86\n"
    )
    # A 30-June snapshot carries on_date 1 July; July's EUR rate isn't out.
    raws = [
        RawHolding("A", date(2026, 7, 1), "LU0000000001", D(100), D(50), "EUR", False),
    ]
    valued = value_holdings(raws, commodities={}, rate_source=rates)
    # 100 × 50 EUR × 0.86 (June, forward-filled) = 4300, no gap.
    assert valued.rate_gaps == ()
    assert valued.gross_long_gbp == D("4300.00")


def test_build_timeline_end_to_end_vanguard() -> None:
    text = _VANGUARD.read_text(encoding="utf-8")
    tl = build_timeline([(text, "vg.txt")], commodities={}, rate_source=NullSource())
    assert len(tl.points) == 1
    # VMIG 13×37.41 + VGVA 25×19.92 + 17 cash.
    expected = D("13.00") * D("37.41") + D("25.00") * D("19.92") + D("17.00")
    assert tl.points[0].net_worth_gbp == expected


def test_cli_writes_timeline(tmp_path: Path) -> None:
    out_dir = tmp_path / "nw"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app,
        [
            "net-worth", "--statement", str(_VANGUARD),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out_dir / "net-worth.md").read_text(encoding="utf-8")
    assert "# Net worth over time" in md
    csv_text = (out_dir / "net-worth.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == (
        "date,gross_long_gbp,net_cash_gbp,net_worth_gbp,change_gbp,portfolios"
    )


_PICTET = Path("tests/fixtures/en/pictet/monthly_statement.txt")


def test_cli_strict_fails_on_unconvertible_snapshot(tmp_path: Path) -> None:
    # Pictet statement has non-GBP holdings; with the null rate source they
    # can't be valued → the timeline understates. --strict must catch it.
    out_dir = tmp_path / "nw"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    base = [
        "net-worth", "--statement", str(_PICTET), "--out", str(out_dir),
        "--commodities", str(commodities), "--rate-source", "null",
    ]
    runner = CliRunner()

    lenient = runner.invoke(cli.app, base)
    assert lenient.exit_code == 0, lenient.output  # writes + warns, doesn't fail

    strict = runner.invoke(cli.app, [*base, "--strict"])
    assert strict.exit_code == 1, strict.output


def test_render_flags_unclassified_missing_price_and_caveat() -> None:
    # One priced-but-unclassified holding (no metadata) and one with no mark.
    raws = [
        RawHolding("A", date(2025, 1, 1), "IE00UNCLASS1", D(10), D(100), "GBP", False),
        RawHolding("A", date(2025, 1, 1), "IE00NOMARK01", D(5), None, "GBP", False),
    ]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    assert tl.unclassified == ("IE00UNCLASS1",)
    assert tl.missing_prices == ("IE00NOMARK01",)

    md = render_markdown(tl)
    assert "Unclassified holdings (no metadata)" in md
    assert "IE00UNCLASS1" in md
    assert "Unvaluable holdings (no statement mark)" in md
    assert "IE00NOMARK01" in md
    assert "wound-down portfolio" in md  # the B6 forward-fill caveat


def test_missing_prices_scoped_to_latest_snapshot() -> None:
    """Unvaluable holdings are reported only for each portfolio's *latest*
    snapshot. A holding the parser couldn't mark in an old statement but
    can in the current one (or that's since been sold) must not linger in
    the warning — it would name long-superseded holdings as 'currently
    unvaluable'."""

    raws = [
        # Old snapshot: HISTONLY had no mark; NOWOK also unmarked then.
        RawHolding("A", date(2025, 1, 1), "IE00HISTONLY", D(5), None, "GBP", False),
        RawHolding("A", date(2025, 1, 1), "IE00NOWOK001", D(5), None, "GBP", False),
        # Latest snapshot: NOWOK now prices; HISTONLY is gone; STILLBAD is new
        # and still unmarked.
        _sec("A", date(2025, 2, 1), "IE00NOWOK001", D(5), D(100)),
        RawHolding("A", date(2025, 2, 1), "IE00STILLBAD", D(5), None, "GBP", False),
    ]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    # Only the latest snapshot's unvaluable holding is surfaced.
    assert tl.missing_prices == ("IE00STILLBAD",)


def test_render_table_is_newest_first() -> None:
    raws = [
        _sec("A", date(2025, 1, 1), "IE00B3VWN518", D(10), D(100)),
        _sec("A", date(2025, 2, 1), "IE00B3VWN518", D(12), D(100)),
        _sec("A", date(2025, 3, 1), "IE00B3VWN518", D(11), D(100)),
    ]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    md = render_markdown(tl)
    dates = [
        ln.split("|")[1].strip()
        for ln in md.splitlines() if ln.startswith("| 2025-")
    ]
    assert dates == ["2025-03-01", "2025-02-01", "2025-01-01"]  # descending
