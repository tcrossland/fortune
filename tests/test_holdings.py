"""Holdings cost-basis / unrealised-P&L report."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.basis_lens import BasisLens, HoldingBasis
from banking_pipeline.fx.gbp_rates import NullSource
from banking_pipeline.holdings import (
    build_report,
    join_holdings,
    render_csv_rows,
    render_markdown,
)
from banking_pipeline.valuation import RawHolding, value_holdings

D = Decimal

_VANGUARD = Path("tests/fixtures/en/vanguard_uk/vanguard_regular_statement.txt")


class _StubLens:
    """A BasisLens over a fixed per-ISIN basis map."""

    name = "stub"
    currency = "GBP"

    def __init__(self, basis: dict[str, HoldingBasis]) -> None:
        self._basis = basis

    def basis_for(self) -> dict[str, HoldingBasis]:
        return self._basis


class _EurStubLens:
    """A non-GBP lens — the reserved ES shape — for the renderer guard."""

    name = "es-stub"
    currency = "EUR"

    def basis_for(self) -> dict[str, HoldingBasis]:
        return {}


def _sec(key: str, qty: Decimal, price: Decimal) -> RawHolding:
    # GBP security so to_gbp is identity (no rate needed).
    return RawHolding("Pic:K", date(2026, 4, 1), key, qty, price, "GBP", False)


def _basis(isin: str, qty: Decimal, cost: Decimal) -> HoldingBasis:
    return HoldingBasis(isin, qty, cost, "GBP", market_value=None)


def _value(raws: list[RawHolding]) -> object:
    return value_holdings(raws, commodities={}, rate_source=NullSource())


def test_join_matches_basis_and_computes_unrealised() -> None:
    held = "IE00B3VWN518"
    isa = "VMIG"  # ticker, no lens basis
    valuation = _value([_sec(held, D("60"), D("20")), _sec(isa, D("10"), D("5"))])
    report = join_holdings(valuation, {held: _basis(held, D("60"), D("600"))})

    by_key = {r.key: r for r in report.rows}
    assert by_key[held].market_value_gbp == D("1200")
    assert by_key[held].cost_basis_gbp == D("600")
    assert by_key[held].unrealised_gbp == D("600")
    assert by_key[held].basis_qty == D("60")
    # ISA ticker has no matched basis → cost / unrealised blank.
    assert by_key[isa].cost_basis_gbp is None
    assert by_key[isa].unrealised_gbp is None

    assert report.total_market_gbp == D("1250")  # every holding
    assert report.total_cost_gbp == D("600")  # matched-basis only
    assert report.total_unrealised_gbp == D("600")
    assert report.qty_drifts == ()
    assert report.unmatched_basis == ()


def test_join_aggregates_same_isin_across_portfolios() -> None:
    # A fund held in two Pictet mandates: the section 104 pool is NIF-level
    # (account-blind), so the two statement rows must consolidate into one and
    # face the pool once — not double-count cost or false-flag a drift.
    held = "IE00B3VWN518"
    valuation = _value([
        RawHolding("Pic:K", date(2026, 4, 1), held, D("100"), D("20"), "GBP", False),
        RawHolding("Pic:P", date(2026, 4, 1), held, D("40"), D("20"), "GBP", False),
    ])
    report = join_holdings(valuation, {held: _basis(held, D("140"), D("1400"))})

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.quantity == D("140")  # 100 + 40 consolidated
    assert row.market_value_gbp == D("2800")  # (100 + 40) × 20
    assert row.cost_basis_gbp == D("1400")  # pool cost counted once
    assert report.total_cost_gbp == D("1400")
    assert report.total_market_gbp == D("2800")
    assert report.qty_drifts == ()  # 140 == 140, no spurious drift


def test_join_flags_quantity_drift() -> None:
    held = "IE00B3VWN518"
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(valuation, {held: _basis(held, D("55"), D("550"))})

    assert len(report.qty_drifts) == 1
    drift = report.qty_drifts[0]
    assert (drift.statement_qty, drift.pool_qty) == (D("60"), D("55"))


def test_join_reports_basis_not_on_statement() -> None:
    held = "IE00B3VWN518"
    ghost = "IE00B4L5Y983"  # lens holds it, no statement marks it
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(
        valuation,
        {held: _basis(held, D("60"), D("600")), ghost: _basis(ghost, D("5"), D("50"))},
    )
    assert report.unmatched_basis == (ghost,)


def test_unrealised_can_be_a_loss() -> None:
    held = "IE00B3VWN518"
    valuation = _value([_sec(held, D("60"), D("10"))])  # market 600
    report = join_holdings(valuation, {held: _basis(held, D("60"), D("900"))})
    assert report.rows[0].unrealised_gbp == D("-300")


def test_render_markdown_shows_amounts_and_blank_cost() -> None:
    held = "IE00B3VWN518"
    isa = "VMIG"
    valuation = _value([_sec(held, D("60"), D("20")), _sec(isa, D("10"), D("5"))])
    md = render_markdown(join_holdings(valuation, {held: _basis(held, D("60"), D("600"))}))
    assert "Holdings — cost basis & unrealised P&L" in md
    assert "£1,200.00" in md  # market value of the Pictet holding
    assert "£600.00" in md  # its cost basis
    assert "| —  " in md or "| — |" in md  # ISA row blank cost/unrealised


def test_render_csv_blank_cells_for_unmatched_basis() -> None:
    held = "IE00B3VWN518"
    isa = "VMIG"
    valuation = _value([_sec(held, D("60"), D("20")), _sec(isa, D("10"), D("5"))])
    rows = render_csv_rows(join_holdings(valuation, {held: _basis(held, D("60"), D("600"))}))
    assert rows[0] == [
        "key", "name", "currency", "quantity", "market_value_gbp",
        "cost_basis_gbp", "unrealised_gbp", "pool_qty",
    ]
    by_key = {r[0]: r for r in rows[1:]}
    assert by_key[held][5:8] == ["600.00", "600.00", "60.00"]
    assert by_key[isa][5:8] == ["", "", ""]  # no matched basis


def test_build_report_end_to_end_wires_statements_and_lens() -> None:
    text = _VANGUARD.read_text(encoding="utf-8")
    report = build_report(
        [(text, "vg.txt")],
        commodities={},
        rate_source=NullSource(),
        basis=_StubLens({}),  # tickers never match an ISIN-keyed lens
    )
    keys = {r.key for r in report.rows}
    assert {"VMIG", "VGVA"} <= keys
    assert report.total_cost_gbp == D("0")  # nothing matched
    assert all(r.cost_basis_gbp is None for r in report.rows)


def test_stub_lens_satisfies_protocol() -> None:
    lens: BasisLens = _StubLens({})
    assert lens.basis_for() == {}


def test_build_report_rejects_non_gbp_lens() -> None:
    # The renderer is GBP-only; a non-GBP lens must be rejected, not
    # mis-rendered as £ (guards the future ES lens).
    with pytest.raises(NotImplementedError, match="GBP only"):
        build_report(
            [], commodities={}, rate_source=NullSource(), basis=_EurStubLens()
        )


# --- CLI -------------------------------------------------------------------


def test_cli_basis_es_is_not_yet_implemented() -> None:
    result = CliRunner().invoke(cli.app, ["holdings", "--basis", "es"])
    assert result.exit_code == 2
    assert "not yet implemented" in result.output


def test_cli_rejects_unknown_basis() -> None:
    result = CliRunner().invoke(cli.app, ["holdings", "--basis", "bogus"])
    assert result.exit_code == 2
    assert "must be 'uk' or 'es'" in result.output


def test_cli_writes_reports(tmp_path: Path) -> None:
    out = tmp_path / "out"
    empty_src = tmp_path / "sidecars"
    empty_src.mkdir()
    result = CliRunner().invoke(
        cli.app,
        [
            "holdings",
            "--statement", str(_VANGUARD),
            "--source", str(empty_src),
            "--opening-positions", str(tmp_path / "none.toml"),
            "--out", str(out),
            "--rate-source", "null",
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out / "holdings.md").read_text(encoding="utf-8")
    assert "cost basis" in md.lower()
    assert (out / "holdings.csv").is_file()
