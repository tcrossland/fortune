"""FIG-window projection: deferring vs. crystallising foreign gains."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.cli.reports import (
    _fig_projection_csv_rows,
    _foreign_holdings,
    _remaining_fig_window,
    _render_fig_projection_md,
)
from banking_pipeline.holdings import HoldingRow
from banking_pipeline.tax.uk.fig_projection import (
    FigProjectionHolding,
    project_fig_window,
)
from banking_pipeline.tax.uk.rates import default_cgt_rates, default_income_bands

D = Decimal
_ZERO = D(0)
BANDS = default_income_bands()
CGT = default_cgt_rates()
YEAR = "2026-27"
ACT_BY = date(2027, 4, 5)


def _project(
    holdings: list[FigProjectionHolding],
    *,
    income: Decimal = _ZERO,
    window: list[str] | None = None,
) -> object:
    return project_fig_window(
        window=window if window is not None else [YEAR],
        act_by=ACT_BY,
        holdings=holdings,
        income=income,
        rate_year=YEAR,
        bands=BANDS[YEAR],
        cgt_rates=CGT[YEAR],
    )


def test_prices_deferred_cgt_by_band_stacking_at_zero_income() -> None:
    # £50k gain, no income: 37,700 of basic band @ 18% + 12,300 @ 24%
    # = 6,786 + 2,952 = £9,738.
    p = _project([FigProjectionHolding("A", "Fund A", D(50000))])
    assert p.crystallisable_gain_gbp == D(50000)
    assert p.deferred_cgt_gbp == D("9738.00")


def test_higher_income_pushes_the_gain_into_the_higher_cgt_rate() -> None:
    # Income fills the basic-rate band, so the whole gain is taxed at 24%.
    p = _project(
        [FigProjectionHolding("A", "Fund A", D(50000))], income=D(60000)
    )
    assert p.deferred_cgt_gbp == D("12000.00")  # 50,000 × 0.24


def test_foreign_losers_excluded_from_crystallisable_but_shown_in_net() -> None:
    # A FIG-relieved loss carries no benefit, so only the winners are priced;
    # the net (winners + losers) is reported for context.
    p = _project([
        FigProjectionHolding("A", "Winner", D(50000)),
        FigProjectionHolding("B", "Loser", D(-10000)),
    ])
    assert p.crystallisable_gain_gbp == D(50000)
    assert p.net_foreign_unrealised_gbp == D(40000)
    assert p.deferred_cgt_gbp == D("9738.00")  # priced on the winner only


def test_no_positive_gains_gives_a_nil_saving() -> None:
    p = _project([FigProjectionHolding("B", "Loser", D(-10000))])
    assert p.crystallisable_gain_gbp == D(0)
    assert p.deferred_cgt_gbp == D(0)


def test_holdings_sorted_by_gain_desc_and_window_passthrough() -> None:
    p = _project(
        [
            FigProjectionHolding("A", "Small", D(1000)),
            FigProjectionHolding("B", "Big", D(9000)),
        ],
        window=["2025-26", "2026-27"],
    )
    assert [h.key for h in p.holdings] == ["B", "A"]
    assert p.window == ["2025-26", "2026-27"]
    assert p.act_by == ACT_BY


# --- render + CLI ----------------------------------------------------------


def test_render_shows_headline_actby_and_caveats() -> None:
    md = _render_fig_projection_md(
        _project([FigProjectionHolding("A", "Fund A", D(50000))]),
        as_of=date(2026, 4, 1),
    )
    assert "Crystallisable foreign gains: £50,000.00" in md
    assert "£9,738.00" in md  # the deferred CGT / saving
    assert "Act by 2027-04-05" in md
    assert "bed-and-breakfast" in md
    assert "not tax advice" in md.lower()
    assert "| Fund A (A) | £50,000.00 |" in md


def test_render_window_closed_variant() -> None:
    closed = project_fig_window(
        window=[], act_by=None,
        holdings=[FigProjectionHolding("A", "Fund A", D(50000))],
        income=_ZERO, rate_year=YEAR, bands=BANDS[YEAR], cgt_rates=CGT[YEAR],
    )
    md = _render_fig_projection_md(closed, as_of=None)
    assert "window has **closed**" in md
    assert "Act by" not in md


def test_csv_rows_sorted_by_gain() -> None:
    rows = _fig_projection_csv_rows(
        _project([
            FigProjectionHolding("A", "Fund A", D(50000)),
            FigProjectionHolding("B", "Loser", D(-100)),
        ])
    )
    assert rows[0] == ["key", "name", "unrealised_gbp"]
    assert rows[1] == ["A", "Fund A", "50000.00"]
    assert rows[2] == ["B", "Loser", "-100.00"]


def test_cli_rejects_non_numeric_income() -> None:
    result = CliRunner().invoke(cli.app, ["fig-projection", "--income", "abc"])
    assert result.exit_code == 1
    assert "must be a number" in result.output


def test_cli_rejects_negative_income() -> None:
    result = CliRunner().invoke(cli.app, ["fig-projection", "--income", "-5000"])
    assert result.exit_code == 1
    assert "must not be negative" in result.output


def test_remaining_window_boundary() -> None:
    # Arrival 2023-07-14 → eligible {2025-26, 2026-27} (regime starts 2025-26).
    arrival = date(2023, 7, 14)
    # Mid-2026-27: 2025-26 has closed (ended 2026-04-05), 2026-27 remains.
    window, act_by = _remaining_fig_window(arrival, date(2026, 7, 5))
    assert window == ["2026-27"]
    assert act_by == date(2027, 4, 5)
    # On the last day of the window the year still counts (end >= today).
    window_last, _ = _remaining_fig_window(arrival, date(2027, 4, 5))
    assert window_last == ["2026-27"]
    # The day after, the window has closed.
    window_closed, act_closed = _remaining_fig_window(arrival, date(2027, 4, 6))
    assert window_closed == []
    assert act_closed is None


def _row(key: str, situs: bool | None, unrealised: Decimal | None) -> HoldingRow:
    return HoldingRow(
        key=key, name=key, currency="GBP", quantity=D(1),
        market_value_gbp=D(0),
        cost_basis_gbp=None if unrealised is None else D(0),
        unrealised_gbp=unrealised, basis_qty=None, eri_uplift_gbp=None,
        uk_situs=situs,
    )


def test_foreign_holdings_filter() -> None:
    rows = [
        _row("F", False, D(100)),   # foreign + basis → included
        _row("U", True, D(50)),     # UK-situs → excluded
        _row("N", None, D(30)),     # unclassified → excluded
        _row("FB", False, None),    # foreign but no matched basis → excluded
    ]
    out = _foreign_holdings(rows)
    assert [h.key for h in out] == ["F"]
    assert out[0].unrealised_gbp == D(100)
