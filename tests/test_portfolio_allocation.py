"""Per-portfolio allocation report."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import NullSource
from banking_pipeline.portfolio_allocation import _report_from_raw, build_report
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


def test_splits_by_portfolio_with_shares() -> None:
    raws = [
        _sec("Assets:Pic:A", date(2025, 1, 1), _EQUITY, D(10), D(100)),  # 1000
        _sec("Assets:Pic:B", date(2025, 1, 1), _BOND, D(30), D(100)),    # 3000
    ]
    report = _report_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    assert report.total_net_worth_gbp == D(4000)
    # Sorted by net worth desc → B (3000) first.
    assert [p.label for p in report.portfolios] == ["Pic:B", "Pic:A"]
    b, a = report.portfolios
    assert b.net_worth_gbp == D(3000)
    assert dict(a.by_class_gbp) == {"equity-etf": D(1000)}
    assert dict(b.by_class_gbp) == {"bond": D(3000)}


def test_label_strips_assets_prefix() -> None:
    raws = [_sec("Assets:Vgd:ISA", date(2025, 1, 1), _EQUITY, D(1), D(50))]
    report = _report_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    assert report.portfolios[0].label == "Vgd:ISA"


def test_latest_snapshot_per_portfolio() -> None:
    raws = [
        _sec("Assets:Pic:A", date(2025, 1, 1), _EQUITY, D(10), D(100)),  # stale
        _sec("Assets:Pic:A", date(2025, 6, 1), _EQUITY, D(15), D(100)),  # latest
    ]
    report = _report_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    assert len(report.portfolios) == 1
    p = report.portfolios[0]
    assert p.as_of == date(2025, 6, 1)
    assert p.net_worth_gbp == D(1500)


def test_cash_netted_within_portfolio_not_across() -> None:
    raws = [
        _sec("Assets:Pic:A", date(2025, 1, 1), _EQUITY, D(100), D(100)),  # 10000
        RawHolding("Assets:Pic:A", date(2025, 1, 1), "GBP", D(-4000), None, "GBP", True),
        _sec("Assets:Pic:B", date(2025, 1, 1), _BOND, D(20), D(100)),     # 2000
        RawHolding("Assets:Pic:B", date(2025, 1, 1), "GBP", D(1000), None, "GBP", True),
    ]
    report = _report_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    by_label = {p.label: p for p in report.portfolios}
    assert by_label["Pic:A"].net_cash_gbp == D(-4000)  # leverage stays on A
    assert by_label["Pic:B"].net_cash_gbp == D(1000)
    # Totals still reconcile with a combined netting.
    assert report.total_net_cash_gbp == D(-3000)
    assert report.total_net_worth_gbp == D(9000)


def test_property_is_its_own_portfolio() -> None:
    raws = [
        _sec("Assets:Pic:A", date(2025, 1, 1), _EQUITY, D(10), D(100)),
        RawHolding(
            "Property:Home", date(2025, 1, 1), "HOME", D(1), D(500000),
            "GBP", False, label="Home", asset_class="property", domicile="GB",
        ),
    ]
    report = _report_from_raw(raws, commodities=_COMMODITIES, rate_source=NullSource())
    labels = {p.label for p in report.portfolios}
    assert "Property:Home" in labels
    home = next(p for p in report.portfolios if p.label == "Property:Home")
    assert dict(home.by_class_gbp) == {"property": D(500000)}


def test_rate_gap_recorded() -> None:
    raws = [_sec("Assets:Pic:A", date(2025, 6, 1), "US0378331005", D(10), D(100),
                 currency="USD")]
    report = _report_from_raw(raws, commodities={}, rate_source=NullSource())
    assert len(report.rate_gaps) == 1
    assert report.rate_gaps[0].currency == "USD"


def test_cli_writes_report(tmp_path: Path) -> None:
    out_dir = tmp_path / "pa"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app,
        [
            "portfolio-allocation", "--statement", str(_VANGUARD),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out_dir / "portfolio-allocation.md").read_text(encoding="utf-8")
    assert "# Portfolio allocation" in md
    csv_text = (out_dir / "portfolio-allocation.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == (
        "portfolio,as_of,asset_class,value_gbp,weight_pct,portfolio_net_worth_gbp"
    )


def test_build_report_end_to_end_vanguard() -> None:
    text = _VANGUARD.read_text(encoding="utf-8")
    report = build_report([(text, "vg.txt")], commodities={}, rate_source=NullSource())
    assert len(report.portfolios) == 1

_PICTET = Path("tests/fixtures/en/pictet/monthly_statement.txt")


def test_cli_strict_fails_on_unvaluable_holding(tmp_path: Path) -> None:
    out_dir = tmp_path / "pa"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    base = [
        "portfolio-allocation", "--statement", str(_PICTET),
        "--out", str(out_dir), "--commodities", str(commodities),
        "--rate-source", "null",
    ]
    runner = CliRunner()
    assert runner.invoke(cli.app, base).exit_code == 0
    assert runner.invoke(cli.app, [*base, "--strict"]).exit_code == 1
