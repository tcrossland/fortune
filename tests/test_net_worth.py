"""Net-worth-over-time timeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.fx.gbp_rates import NullSource
from banking_pipeline.net_worth import _timeline_from_raw, build_timeline
from banking_pipeline.valuation import RawHolding

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
