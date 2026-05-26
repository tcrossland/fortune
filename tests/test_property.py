"""Off-ledger residential property: loader, ledger generation, reports."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.concentration import build_report
from banking_pipeline.fx.gbp_rates import NullSource
from banking_pipeline.net_worth import build_timeline
from banking_pipeline.property import (
    Property,
    load_properties,
    render_beancount,
)

D = Decimal


class _FakeRates:
    def __init__(self, rates: dict[str, Decimal]) -> None:
        self._rates = rates

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return self._rates.get(currency)


def _prop(**kw: object) -> Property:
    base: dict[str, object] = dict(
        label="Bristol", country="GB", currency="GBP",
        acquired=date(2025, 9, 1), purchase_price=D("500000.00"),
    )
    base.update(kw)
    return Property.model_validate(base)


def test_commodity_derived_from_label() -> None:
    assert _prop(label="Torrelodones").commodity == "TORRELODONES"
    assert _prop(label="Flat 2").commodity == "FLAT2"
    # Explicit commodity is kept.
    assert _prop(commodity="BTL1").commodity == "BTL1"


def test_marks_seed_purchase_on_acquisition() -> None:
    p = _prop(valuations=[{"date": date(2026, 1, 1), "value": D("520000")}])
    marks = p.marks()
    assert [m.date for m in marks] == [date(2025, 9, 1), date(2026, 1, 1)]
    assert marks[0].value == D("500000.00")  # seeded purchase price


def test_load_properties_roundtrip(tmp_path: Path) -> None:
    toml = tmp_path / "property.toml"
    toml.write_text(
        '[[property]]\nlabel = "Torrelodones"\ncountry = "ES"\n'
        'currency = "EUR"\nacquired = 2025-10-01\npurchase_price = 400000.00\n',
        encoding="utf-8",
    )
    props = load_properties(toml)
    assert len(props) == 1
    assert props[0].commodity == "TORRELODONES"
    assert props[0].currency == "EUR"


def test_render_gbp_property() -> None:
    out = render_beancount([_prop()], rate_source=NullSource())
    assert "2025-09-01 commodity BRISTOL" in out
    assert "2025-09-01 open Assets:Property:Bristol BRISTOL" in out
    assert "2025-09-01 open Equity:Property:Bristol" in out
    assert "Assets:Property:Bristol  1 BRISTOL {500,000.00 GBP}" in out
    assert "2025-09-01 price BRISTOL  500,000.00 GBP" in out


def test_render_eur_property_emits_gbp_mark() -> None:
    torre = _prop(
        label="Torrelodones", country="ES", currency="EUR",
        acquired=date(2025, 10, 1), purchase_price=D("400000.00"),
    )
    out = render_beancount([torre], rate_source=_FakeRates({"EUR": D("0.85")}))
    assert "1 TORRELODONES {400,000.00 EUR}" in out
    assert "price TORRELODONES  400,000.00 EUR" in out
    assert "price TORRELODONES  340,000.00 GBP" in out  # 400000 * 0.85


def test_render_eur_property_warns_without_rate() -> None:
    torre = _prop(
        label="Torrelodones", country="ES", currency="EUR",
        acquired=date(2025, 10, 1), purchase_price=D("400000.00"),
    )
    out = render_beancount([torre], rate_source=NullSource())
    assert "price TORRELODONES  400,000.00 EUR" in out
    assert "; WARN no GBP rate for EUR" in out
    assert "GBP" not in out.split("price TORRELODONES  400,000.00 EUR")[1].split("\n")[0]


def test_property_in_concentration() -> None:
    torre = _prop(
        label="Torrelodones", country="ES", currency="EUR",
        acquired=date(2025, 10, 1), purchase_price=D("400000.00"),
    )
    report = build_report(
        [], commodities={}, rate_source=_FakeRates({"EUR": D("0.85")}),
        properties=[_prop(), torre],
    )
    by_key = {h.key: h for h in report.securities}
    assert by_key["BRISTOL"].value_gbp == D("500000.00")
    assert by_key["BRISTOL"].asset_class == "property"
    assert by_key["BRISTOL"].domicile == "GB"
    assert by_key["TORRELODONES"].value_gbp == D("340000.00")  # 400000 * 0.85
    assert by_key["TORRELODONES"].domicile == "ES"
    # Property carries its own metadata, so it isn't flagged unclassified.
    assert report.unclassified == ()
    assert report.gross_long_gbp == D("840000.00")


def test_property_in_net_worth_timeline() -> None:
    p = _prop(valuations=[{"date": date(2026, 1, 1), "value": D("550000")}])
    tl = build_timeline([], commodities={}, rate_source=NullSource(), properties=[p])
    # Two marks → two timeline points; value steps from purchase to revaluation.
    assert [pt.on_date for pt in tl.points] == [date(2025, 9, 1), date(2026, 1, 1)]
    assert tl.points[0].net_worth_gbp == D("500000")
    assert tl.points[1].net_worth_gbp == D("550000")


def test_cli_property_generates_ledger(tmp_path: Path) -> None:
    src = tmp_path / "property.toml"
    src.write_text(
        '[[property]]\nlabel = "Bristol"\ncountry = "GB"\ncurrency = "GBP"\n'
        "acquired = 2025-09-01\npurchase_price = 500000.00\n",
        encoding="utf-8",
    )
    out = tmp_path / "property.beancount"
    result = CliRunner().invoke(
        cli.app, ["property", "--source", str(src), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert "commodity BRISTOL" in text
    assert "Assets:Property:Bristol" in text
