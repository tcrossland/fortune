"""Asset-allocation-over-time timeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.allocation import (
    _timeline_from_raw,
    build_timeline,
    render_markdown,
)
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import NullSource
from banking_pipeline.valuation import RawHolding

D = Decimal

_VANGUARD = Path("tests/fixtures/en/vanguard_uk/vanguard_regular_statement.txt")


def _sec(
    portfolio: str, on: date, key: str, qty: Decimal, price: Decimal,
    currency: str = "GBP",
) -> RawHolding:
    return RawHolding(portfolio, on, key, qty, price, currency, False)


def _meta(isin: str, asset_class: str) -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin, name=asset_class.title(), domicile="GB",
        reporting_status="reporting", asset_class=asset_class,
        first_acquired=date(2020, 1, 1),
    )


_EQUITY = "IE00B3VWN518"
_BOND = "LU1287023185"
_COMMODITIES = {_EQUITY: _meta(_EQUITY, "equity-etf"), _BOND: _meta(_BOND, "bond")}


def test_by_class_breakdown_and_ordering() -> None:
    raws = [
        _sec("A", date(2025, 1, 1), _EQUITY, D(10), D(100)),  # equity 1000
        _sec("A", date(2025, 1, 1), _BOND, D(5), D(100)),     # bond 500
    ]
    tl = _timeline_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    assert tl.asset_classes == ("equity-etf", "bond")  # preferred order
    p = tl.points[0]
    assert p.gross_long_gbp == D(1500)
    assert dict(p.by_class_gbp) == {"equity-etf": D(1000), "bond": D(500)}


def test_forward_fill_tracks_class_drift() -> None:
    """Equity grows while bond is forward-filled, so the mix drifts."""

    raws = [
        _sec("EQ", date(2025, 1, 1), _EQUITY, D(10), D(100)),  # equity 1000
        _sec("BD", date(2025, 2, 1), _BOND, D(5), D(100)),     # bond 500
        _sec("EQ", date(2025, 3, 1), _EQUITY, D(20), D(100)),  # equity 2000
    ]
    tl = _timeline_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    pts = {p.on_date: p for p in tl.points}
    # At Feb: equity forward-filled 1000 + bond 500.
    assert dict(pts[date(2025, 2, 1)].by_class_gbp) == {"equity-etf": D(1000), "bond": D(500)}
    assert pts[date(2025, 2, 1)].portfolios == 2
    # At Mar: equity updates to 2000, bond forward-filled 500.
    assert dict(pts[date(2025, 3, 1)].by_class_gbp) == {"equity-etf": D(2000), "bond": D(500)}


def test_unknown_class_sorts_last() -> None:
    raws = [
        _sec("A", date(2025, 1, 1), _EQUITY, D(10), D(100)),
        _sec("A", date(2025, 1, 1), "XX0000000000", D(10), D(100)),  # no metadata
    ]
    tl = _timeline_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    assert tl.asset_classes == ("equity-etf", "unknown")


def test_leverage_negative_net_cash() -> None:
    raws = [
        _sec("A", date(2025, 1, 1), _EQUITY, D(100), D(100)),  # equity 10000
        RawHolding("A", date(2025, 1, 1), "GBP", D(-6000), None, "GBP", True),
    ]
    tl = _timeline_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    p = tl.points[0]
    assert p.gross_long_gbp == D(10000)
    assert p.net_cash_gbp == D(-6000)
    assert p.net_worth_gbp == D(4000)
    # Security classes still measure against gross long, undistorted by cash.
    assert dict(p.by_class_gbp) == {"equity-etf": D(10000)}


def test_property_folded_in_as_class() -> None:
    raws = [
        _sec("A", date(2025, 1, 1), _EQUITY, D(10), D(100)),  # equity 1000
        RawHolding(
            "Property:Home", date(2025, 1, 1), "HOME", D(1), D(500000),
            "GBP", False, label="Home", asset_class="property", domicile="GB",
        ),
    ]
    tl = _timeline_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    p = tl.points[0]
    assert dict(p.by_class_gbp)["property"] == D(500000)
    assert "property" in tl.asset_classes


def test_rate_gap_excluded_and_recorded() -> None:
    # USD holding, NullSource → unconvertible, excluded + flagged.
    raws = [_sec("A", date(2025, 6, 1), "US0378331005", D(10), D(100), currency="USD")]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    p = tl.points[0]
    assert p.gross_long_gbp == D(0)
    assert len(tl.rate_gaps) == 1
    assert tl.rate_gaps[0].currency == "USD"


def test_cli_writes_report(tmp_path: Path) -> None:
    out_dir = tmp_path / "alloc"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app,
        [
            "allocation", "--statement", str(_VANGUARD),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out_dir / "allocation.md").read_text(encoding="utf-8")
    assert "# Asset allocation over time" in md
    csv_text = (out_dir / "allocation.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == (
        "date,asset_class,value_gbp,weight_pct,gross_long_gbp,net_worth_gbp"
    )


def test_build_timeline_end_to_end_vanguard() -> None:
    text = _VANGUARD.read_text(encoding="utf-8")
    tl = build_timeline([(text, "vg.txt")], commodities={}, rate_source=NullSource())
    assert len(tl.points) == 1
    # Vanguard tickers carry no commodities.toml entry → unknown class.
    assert "unknown" in tl.asset_classes


def test_render_flags_unclassified_and_caveat() -> None:
    # A priced holding with no metadata → unknown asset-class bucket AND the
    # unclassified warning (previously dropped by allocation).
    raws = [_sec("A", date(2025, 1, 1), "IE00UNCLASS1", D(10), D(100))]
    tl = _timeline_from_raw(raws, commodities={}, rate_source=NullSource())
    assert tl.unclassified == ("IE00UNCLASS1",)

    md = render_markdown(tl)
    assert "Unclassified holdings (no metadata)" in md
    assert "IE00UNCLASS1" in md
    assert "wound-down portfolio" in md  # the B6 forward-fill caveat


_PICTET = Path("tests/fixtures/en/pictet/monthly_statement.txt")


def test_cli_strict_fails_on_unvaluable_holding(tmp_path: Path) -> None:
    out_dir = tmp_path / "alloc"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    base = [
        "allocation", "--statement", str(_PICTET), "--out", str(out_dir),
        "--commodities", str(commodities), "--rate-source", "null",
    ]
    runner = CliRunner()
    assert runner.invoke(cli.app, base).exit_code == 0
    assert runner.invoke(cli.app, [*base, "--strict"]).exit_code == 1
