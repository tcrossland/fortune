"""FIG relief + split-year residence in the CGT loss-carryforward chain."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.cgt_allowance import loss_carryforward_chain
from banking_pipeline.tax.uk.sa108 import Sa108Row

D = Decimal
AEA = {"2024-25": D(3000), "2025-26": D(3000)}


def _row(
    *,
    gain: Decimal,
    on: date,
    status: str = "reporting",
    is_foreign: bool = True,
    isin: str = "X",
) -> Sa108Row:
    return Sa108Row(
        disposal_date=on,
        isin=isin,
        commodity_name="",
        reporting_status=status,
        quantity=D(1),
        proceeds_gbp=D(0),
        cost_gbp=D(0),
        gain_gbp=gain,
        match_type="s104",
        acquisition_dates=[],
        is_foreign=is_foreign,
    )


def test_fig_claim_relieves_foreign_gains_and_forfeits_aea() -> None:
    rows = [
        _row(gain=D(10000), on=date(2025, 6, 1), is_foreign=True),
        _row(gain=D(5000), on=date(2025, 7, 1), status="uk-domestic",
             is_foreign=False, isin="GB"),
    ]
    chain = loss_carryforward_chain(
        rows,
        through_year="2025-26",
        aea_by_year=AEA,
        rate_change_dates={},
        arrival=date(2025, 4, 6),
        fig_claim_years=frozenset({"2025-26"}),
    )
    r = chain["2025-26"]
    # Foreign 10k relieved; only the UK-situs 5k is chargeable; AEA forfeited.
    assert r.annual_exempt_amount == D(0)
    assert r.taxable_total == D(5000)


def test_without_claim_both_gains_taxed_with_aea() -> None:
    rows = [
        _row(gain=D(10000), on=date(2025, 6, 1), is_foreign=True),
        _row(gain=D(5000), on=date(2025, 7, 1), status="uk-domestic",
             is_foreign=False, isin="GB"),
    ]
    chain = loss_carryforward_chain(
        rows,
        through_year="2025-26",
        aea_by_year=AEA,
        rate_change_dates={},
        arrival=date(2025, 4, 6),
        fig_claim_years=frozenset(),
    )
    r = chain["2025-26"]
    # 15k gains − 3k AEA = 12k taxable.
    assert r.annual_exempt_amount == D(3000)
    assert r.taxable_total == D(12000)


def test_pre_residence_disposal_dropped_from_chain() -> None:
    rows = [
        # Disposed while non-resident (before arrival) → not UK-taxable.
        _row(gain=D(8000), on=date(2024, 6, 1), is_foreign=True),
        _row(gain=D(4000), on=date(2025, 6, 1), is_foreign=False,
             status="uk-domestic", isin="GB"),
    ]
    chain = loss_carryforward_chain(
        rows,
        through_year="2025-26",
        aea_by_year=AEA,
        rate_change_dates={},
        arrival=date(2025, 4, 6),
        fig_claim_years=frozenset(),
    )
    # The pre-residence 2024-25 year never enters the chain.
    assert "2024-25" not in chain
    assert chain["2025-26"].taxable_total == D(1000)  # 4000 − 3000 AEA
