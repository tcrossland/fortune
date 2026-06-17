"""Portfolio concentration / exposure report."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.concentration import _build_from_raw, build_report
from banking_pipeline.fx.gbp_rates import NullSource
from banking_pipeline.valuation import RawHolding

D = Decimal

_VANGUARD = Path("tests/fixtures/en/vanguard_uk/vanguard_regular_statement.txt")


class _FakeRates:
    """A GbpRateSource stub — fixed per-currency rates, date ignored."""

    def __init__(self, rates: dict[str, Decimal]) -> None:
        self._rates = rates

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return self._rates.get(currency)


def _raw(
    *, portfolio: str = "Pic:K", on: date = date(2026, 4, 1), key: str,
    qty: Decimal, price: Decimal | None, ccy: str, cash: bool = False,
) -> RawHolding:
    return RawHolding(portfolio, on, key, qty, price, ccy, cash)


def test_vanguard_end_to_end_values_and_unclassified() -> None:
    """The Vanguard ISA fixture: two ticker holdings (no metadata → unknown
    + unclassified) valued at qty × GBP mark, plus the cash row."""

    text = _VANGUARD.read_text(encoding="utf-8")
    report = build_report(
        [(text, "vg.txt")], commodities={}, rate_source=NullSource()
    )

    by_key = {h.key: h for h in report.securities}
    assert set(by_key) == {"VMIG", "VGVA"}
    assert by_key["VMIG"].value_gbp == D("13.00") * D("37.41")
    assert by_key["VGVA"].value_gbp == D("25.00") * D("19.92")
    # No metadata for tickers → unknown buckets, flagged.
    assert by_key["VMIG"].asset_class == "unknown"
    assert set(report.unclassified) == {"VGVA", "VMIG"}
    # Cash row, GBP, positive — no leverage here.
    assert len(report.cash) == 1
    assert report.cash[0].currency == "GBP"
    assert report.cash[0].value_gbp == D("17.00")
    assert report.net_cash_gbp == D("17.00")
    assert report.gross_long_gbp == by_key["VMIG"].value_gbp + by_key["VGVA"].value_gbp
    assert report.net_worth_gbp == report.gross_long_gbp + D("17.00")


def test_leverage_and_cross_portfolio_cash_netting() -> None:
    """Negative cash (a Lombard loan) nets across portfolios by currency and
    is excluded from the concentration weights, which are a share of gross
    long holdings."""

    raws = [
        _raw(key="IE00B3VWN518", qty=D(10), price=D(100), ccy="GBP"),  # 1000
        _raw(key="LU1287023185", qty=D(5), price=D(200), ccy="EUR"),   # 1000 EUR
        _raw(key="GBP", qty=D(-1500), price=None, ccy="GBP", cash=True),
        _raw(portfolio="Pic:P", key="GBP", qty=D(100), price=None, ccy="GBP", cash=True),
        _raw(key="EUR", qty=D(250), price=None, ccy="EUR", cash=True),
    ]
    report = _build_from_raw(
        raws, commodities={}, rate_source=_FakeRates({"EUR": D("0.8")})
    )

    assert report.gross_long_gbp == D(1000) + D(800)  # EUR sec @0.8
    # GBP cash nets -1500 + 100 = -1400; EUR cash 250 @0.8 = 200.
    assert report.net_cash_gbp == D(-1400) + D(200)
    assert report.net_worth_gbp == D(1800) + D(-1200)
    # GBP cash netted to a single row, not one per portfolio.
    gbp_cash = [c for c in report.cash if c.currency == "GBP"]
    assert len(gbp_cash) == 1
    assert gbp_cash[0].value_gbp == D(-1400)


def test_latest_statement_per_portfolio_wins() -> None:
    """An older snapshot is superseded by the latest one for the same
    portfolio — a position sold since doesn't linger."""

    raws = [
        _raw(on=date(2025, 1, 1), key="IE00B3VWN518", qty=D(10), price=D(100), ccy="GBP"),
        _raw(on=date(2025, 1, 1), key="LU1287023185", qty=D(5), price=D(50), ccy="GBP"),
        # Later statement: only the first holding remains.
        _raw(on=date(2026, 1, 1), key="IE00B3VWN518", qty=D(7), price=D(110), ccy="GBP"),
    ]
    report = _build_from_raw(raws, commodities={}, rate_source=NullSource())
    assert {h.key for h in report.securities} == {"IE00B3VWN518"}
    assert report.securities[0].value_gbp == D(7) * D(110)
    assert report.as_of == date(2026, 1, 1)


def test_rate_gap_and_missing_price_excluded_and_flagged() -> None:
    raws = [
        _raw(key="IE00B3VWN518", qty=D(10), price=D(100), ccy="GBP"),  # ok
        _raw(key="LU1287023185", qty=D(5), price=D(200), ccy="EUR"),   # no EUR rate
        _raw(key="US0378331005", qty=D(3), price=None, ccy="USD"),     # no mark
    ]
    report = _build_from_raw(raws, commodities={}, rate_source=NullSource())
    assert {h.key for h in report.securities} == {"IE00B3VWN518"}
    assert [g.isin for g in report.rate_gaps] == ["LU1287023185"]
    assert report.missing_prices == ("US0378331005",)


def test_metadata_drives_asset_class_and_domicile() -> None:
    meta = {
        "IE00B3VWN518": CommodityMetadata(
            isin="IE00B3VWN518", name="World ETF", domicile="IE",
            asset_class="equity-etf", reporting_status="reporting",
            first_acquired=date(2020, 1, 1),
        ),
    }
    raws = [_raw(key="IE00B3VWN518", qty=D(10), price=D(100), ccy="GBP")]
    report = _build_from_raw(raws, commodities=meta, rate_source=NullSource())
    h = report.securities[0]
    assert h.name == "World ETF"
    assert h.asset_class == "equity-etf"
    assert h.domicile == "IE"
    assert report.unclassified == ()


def test_cli_writes_report_and_csv(tmp_path: Path) -> None:
    out_dir = tmp_path / "conc"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app,
        [
            "concentration", "--statement", str(_VANGUARD),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out_dir / "concentration.md").read_text(encoding="utf-8")
    assert "# Portfolio concentration" in md
    assert "By holding" in md
    csv_text = (out_dir / "holdings.csv").read_text(encoding="utf-8")
    assert "VMIG" in csv_text
    assert csv_text.splitlines()[0].startswith("kind,key,name")


def test_issuer_inferred_overridden_and_tabulated() -> None:
    meta = {
        # Issuer inferred from the name.
        "IE00B3VWN518": CommodityMetadata(
            isin="IE00B3VWN518", name="iShares Core World", domicile="IE",
            asset_class="equity-etf", reporting_status="reporting",
            first_acquired=date(2020, 1, 1),
        ),
        # Explicit issuer overrides the (mis-)inferable name.
        "LU1287023185": CommodityMetadata(
            isin="LU1287023185", name="Some Fund", domicile="LU",
            asset_class="bond", reporting_status="reporting",
            first_acquired=date(2020, 1, 1), issuer="Amundi",
        ),
    }
    raws = [
        _raw(key="IE00B3VWN518", qty=D(10), price=D(100), ccy="GBP"),
        _raw(key="LU1287023185", qty=D(10), price=D(50), ccy="GBP"),
        _raw(key="US0378331005", qty=D(1), price=D(40), ccy="GBP"),  # no meta
    ]
    report = _build_from_raw(raws, commodities=meta, rate_source=NullSource())
    issuers = {h.key: h.issuer for h in report.securities}
    assert issuers["IE00B3VWN518"] == "iShares"
    assert issuers["LU1287023185"] == "Amundi"
    assert issuers["US0378331005"] == "unknown"  # no metadata → unknown bucket

    from banking_pipeline.concentration import render_csv_rows, render_markdown

    md = render_markdown(report)
    assert "## By issuer" in md
    # Domicile is kept (load-bearing for UK tax), not replaced.
    assert "## By domicile" in md

    header = render_csv_rows(report)[0]
    assert "issuer" in header


_PICTET = Path("tests/fixtures/en/pictet/monthly_statement.txt")


def test_cli_strict_fails_on_unvaluable_holding(tmp_path: Path) -> None:
    out_dir = tmp_path / "conc"
    commodities = tmp_path / "commodities.toml"
    commodities.write_text("", encoding="utf-8")
    base = [
        "concentration", "--statement", str(_PICTET), "--out", str(out_dir),
        "--commodities", str(commodities), "--rate-source", "null",
    ]
    runner = CliRunner()
    assert runner.invoke(cli.app, base).exit_code == 0  # writes + warns
    assert runner.invoke(cli.app, [*base, "--strict"]).exit_code == 1
