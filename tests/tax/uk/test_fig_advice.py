"""Multi-year FIG claim optimisation (loss-aware, across the window)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.fig_advice import (
    FigYearInputs,
    evaluate_fig_window,
)
from banking_pipeline.tax.uk.rates import default_cgt_rates, default_income_bands
from banking_pipeline.tax.uk.sa108 import Sa108Row

D = Decimal
BANDS = default_income_bands()
CGT = default_cgt_rates()
AEA = {"2024-25": D(3000), "2025-26": D(3000), "2026-27": D(3000)}


def _inputs(year: str) -> FigYearInputs:
    return FigYearInputs(
        year=year, other_income=D(0), uk_other=D(0), foreign_other=D(0),
        dividend_income=D(0), dividend_wht=D(0), interest_income=D(0),
        interest_wht=D(0), bands=BANDS[year], cgt_rates=CGT[year],
    )


def _disposal(*, gain: Decimal, on: date) -> Sa108Row:
    return Sa108Row(
        disposal_date=on, isin="LU0000000000", commodity_name="Foreign fund",
        reporting_status="reporting", quantity=D(1), proceeds_gbp=D(0),
        cost_gbp=D(0), gain_gbp=gain, match_type="s104", acquisition_dates=[],
        is_foreign=True,
    )


def test_claiming_the_gain_year_beats_claiming_the_loss_year() -> None:
    # 2025-26: a £20k foreign loss; 2026-27: a £50k foreign gain. No income.
    # Claiming the loss year throws away relief that would shelter the gain;
    # claiming the gain year relieves it entirely. The optimiser should pick
    # the gain year alone.
    window = ["2025-26", "2026-27"]
    year_inputs = {y: _inputs(y) for y in window}
    history = [
        _disposal(gain=D(-20000), on=date(2025, 6, 1)),
        _disposal(gain=D(50000), on=date(2026, 6, 1)),
    ]
    patterns = evaluate_fig_window(
        window=window, year_inputs=year_inputs, history_rows=history,
        aea_by_year=AEA, rate_change_dates={}, pre_ledger_losses=D(0),
        arrival=date(2025, 4, 6),
    )
    by_claim = {p.claimed: p.total_liability for p in patterns}

    # Claim the gain year → the £50k gain is relieved → £0.
    assert by_claim[frozenset({"2026-27"})] == D("0.00")
    # Claim none → the £20k loss shelters the gain: (50k − 20k − 3k AEA) =
    # 27k @ 18% = £4,860.
    assert by_claim[frozenset()] == D("4860.00")
    # Claim only the loss year → loss disallowed, gain fully taxed:
    # (50k − 3k) = 47k → 37.7k @ 18% + 9.3k @ 24% = £9,018. The worst option.
    assert by_claim[frozenset({"2025-26"})] == D("9018.00")

    # Cheapest-first, ties broken toward fewer claims → recommend the gain
    # year alone (not "claim all", which also nets £0).
    assert patterns[0].claimed == frozenset({"2026-27"})
    assert patterns[0].total_liability == D("0.00")


def test_window_of_one_year_two_patterns() -> None:
    window = ["2025-26"]
    patterns = evaluate_fig_window(
        window=window, year_inputs={"2025-26": _inputs("2025-26")},
        history_rows=[_disposal(gain=D(40000), on=date(2025, 6, 1))],
        aea_by_year=AEA, rate_change_dates={}, pre_ledger_losses=D(0),
        arrival=date(2025, 4, 6),
    )
    # Two subsets: claim and don't. Claiming relieves the £40k gain → £0.
    assert {p.claimed for p in patterns} == {frozenset(), frozenset({"2025-26"})}
    assert patterns[0].claimed == frozenset({"2025-26"})
    assert patterns[0].total_liability == D("0.00")
