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
    _post_statement_movement,
    build_report,
    join_holdings,
    render_csv_rows,
    render_markdown,
)
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.valuation import RawHolding, value_holdings

D = Decimal


def _trade(
    *, isin: str, qty: Decimal, doc: DocumentType, trade: date, settle: date | None
) -> Transaction:
    return Transaction(
        trade_date=trade,
        settlement_date=settle,
        booking_date=trade,
        narration="Trade",
        title="Trade",
        currency="GBP",
        amount=qty,  # sign irrelevant to the movement calc (doc_type drives it)
        isin=isin,
        quantity=qty,
        document_type=doc,
        source_path=Path("t.pdf"),
    )

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
    # No movement supplied → nothing explains the drift → a gap.
    assert drift.kind == "gap"
    assert drift.movement == D("0")


def test_drift_classified_timing_when_movement_explains_it() -> None:
    # Statement 60, pool 55: the pool is 5 units light. A post-statement sale
    # of 5 (movement −5 = pool − statement) explains it → timing, not a gap.
    held = "IE00B3VWN518"
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(
        valuation, {held: _basis(held, D("55"), D("550"))}, movement={held: D("-5")}
    )
    drift = report.qty_drifts[0]
    assert drift.kind == "timing"
    assert drift.movement == D("-5")


def test_drift_stays_gap_when_movement_is_partial() -> None:
    # Pool is 5 light but only a 3-unit post-statement sale is ingested — the
    # remaining 2 are unexplained, so it must still read as a gap.
    held = "IE00B3VWN518"
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(
        valuation, {held: _basis(held, D("55"), D("550"))}, movement={held: D("-3")}
    )
    assert report.qty_drifts[0].kind == "gap"


def test_join_reports_basis_not_on_statement() -> None:
    held = "IE00B3VWN518"
    ghost = "IE00B4L5Y983"  # lens holds it, no statement marks it
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(
        valuation,
        {held: _basis(held, D("60"), D("600")), ghost: _basis(ghost, D("5"), D("50"))},
    )
    assert report.unmatched_basis == (ghost,)
    assert report.unmatched_kind[ghost] == "gap"  # no movement to explain it


def test_unmatched_basis_classified_timing_for_post_statement_buy() -> None:
    # A ghost the lens holds at 5 units, none on the statement, wholly acquired
    # after the latest statement (movement +5) → timing, not a stale-statement
    # gap.
    held = "IE00B3VWN518"
    ghost = "IE00B4L5Y983"
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(
        valuation,
        {held: _basis(held, D("60"), D("600")), ghost: _basis(ghost, D("5"), D("50"))},
        movement={ghost: D("5")},
    )
    assert report.unmatched_kind[ghost] == "timing"


def test_post_statement_movement_uses_settlement_date_and_signs_by_doctype() -> None:
    # A month-end statement dated the 1st; a sale traded on that day but
    # settling later is NOT on the mark — settlement date (not trade date)
    # decides, so it counts as a post-statement move. A sell is negative.
    isin = "IE00B3VWN518"
    stmt = date(2026, 7, 1)
    sell = _trade(
        isin=isin, qty=D("3072"), doc=DocumentType.REDEMPTION_NOTICE,
        trade=date(2026, 7, 1), settle=date(2026, 7, 6),
    )
    mv = _post_statement_movement([sell], {isin: stmt}, stmt)
    assert mv == {isin: D("-3072")}


def test_post_statement_movement_excludes_already_settled_and_buys_positive() -> None:
    isin = "IE00B3VWN518"
    stmt = date(2026, 7, 1)
    settled_before = _trade(
        isin=isin, qty=D("100"), doc=DocumentType.SUBSCRIPTION_NOTICE,
        trade=date(2026, 6, 20), settle=date(2026, 6, 24),  # on the mark already
    )
    buy_after = _trade(
        isin=isin, qty=D("838"), doc=DocumentType.SUBSCRIPTION_NOTICE,
        trade=date(2026, 6, 30), settle=date(2026, 7, 3),  # not yet marked
    )
    mv = _post_statement_movement([settled_before, buy_after], {isin: stmt}, stmt)
    assert mv == {isin: D("838")}  # only the post-statement buy, positive


def test_post_statement_movement_falls_back_to_trade_date_without_settlement() -> None:
    isin = "IE00B3VWN518"
    stmt = date(2026, 7, 1)
    no_settle = _trade(
        isin=isin, qty=D("10"), doc=DocumentType.REDEMPTION_NOTICE,
        trade=date(2026, 7, 4), settle=None,
    )
    assert _post_statement_movement([no_settle], {isin: stmt}, stmt) == {isin: D("-10")}


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


def test_render_markdown_shows_drift_status_and_timing_caveat() -> None:
    held = "IE00B3VWN518"
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(
        valuation, {held: _basis(held, D("55"), D("550"))}, movement={held: D("-5")}
    )
    md = render_markdown(report)
    assert "| Holding | Statement qty | Pool qty | Post-stmt trades | Status |" in md
    assert "| timing |" in md
    assert "mixes bases" in md  # the #2 provisional-unrealised caveat


def test_render_markdown_gap_drift_has_no_timing_caveat() -> None:
    held = "IE00B3VWN518"
    valuation = _value([_sec(held, D("60"), D("20"))])
    report = join_holdings(valuation, {held: _basis(held, D("55"), D("550"))})
    md = render_markdown(report)
    assert "| gap |" in md
    assert "to investigate" in md
    assert "mixes bases" not in md  # no timing row → no provisional caveat


def test_render_csv_blank_cells_for_unmatched_basis() -> None:
    held = "IE00B3VWN518"
    isa = "VMIG"
    valuation = _value([_sec(held, D("60"), D("20")), _sec(isa, D("10"), D("5"))])
    rows = render_csv_rows(join_holdings(valuation, {held: _basis(held, D("60"), D("600"))}))
    assert rows[0] == [
        "key", "name", "currency", "quantity", "market_value_gbp",
        "cost_basis_gbp", "eri_uplift_gbp", "unrealised_gbp", "pool_qty",
    ]
    by_key = {r[0]: r for r in rows[1:]}
    # cost, eri (0 — the stub basis has no adjustment), unrealised, pool.
    assert by_key[held][5:9] == ["600.00", "0.00", "600.00", "60.00"]
    assert by_key[isa][5:9] == ["", "", "", ""]  # no matched basis


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
