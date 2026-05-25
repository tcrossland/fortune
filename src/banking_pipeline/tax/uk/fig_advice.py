"""Multi-year Foreign Income & Gains (FIG) claim optimisation.

A FIG claim is decided per year, but the years interact: claiming a year
relieves its foreign income and non-UK gains (and forfeits that year's
personal allowance + CGT annual exempt amount), *and* disallows that
year's foreign losses — losses that would otherwise carry forward and
shelter gains in later years. So the cheapest set of years to claim isn't
the per-year "is this year cheaper claimed?" answer; it's a joint choice
across the eligible window.

The eligible window is at most four tax years, so this brute-forces all
``2^k`` claim subsets. For each subset it runs the loss-carry-forward
chain once (which threads disallowed foreign losses correctly for that
scenario), computes each window year's liability, and sums them. The
cheapest total wins. ``evaluate_fig_window`` is pure; the CLI supplies the
per-year inputs and renders the result.

This is year-to-date *actuals*: an incomplete year's figures grow as more
statements land, so a recommendation touching the current year is
provisional. Not tax advice.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.cgt_allowance import loss_carryforward_chain
from banking_pipeline.tax.uk.liability import compute_liability
from banking_pipeline.tax.uk.rates import CgtRateSchedule, IncomeTaxBands
from banking_pipeline.tax.uk.sa108 import Sa108Row


@dataclass(frozen=True)
class FigYearInputs:
    """One window year's inputs to the liability calc (claim-independent).

    The figures are the same whether or not the year is claimed — the
    claim flag, applied in :func:`evaluate_fig_window`, decides whether the
    foreign items are relieved.
    """

    year: str
    other_income: Decimal
    uk_other: Decimal  # UK-situs income-charged gains (always taxed)
    foreign_other: Decimal  # foreign income-charged gains (relieved if claimed)
    dividend_income: Decimal
    dividend_wht: Decimal
    interest_income: Decimal
    interest_wht: Decimal
    bands: IncomeTaxBands
    cgt_rates: CgtRateSchedule


@dataclass(frozen=True)
class FigPattern:
    """A candidate set of years to claim and the resulting window total."""

    claimed: frozenset[str]
    total_liability: Decimal
    per_year: dict[str, Decimal]


def _subsets(items: list[str]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for r in range(len(items) + 1):
        out.extend(itertools.combinations(items, r))
    return out


def evaluate_fig_window(
    *,
    window: list[str],
    year_inputs: dict[str, FigYearInputs],
    history_rows: list[Sa108Row],
    aea_by_year: dict[str, Decimal],
    rate_change_dates: dict[str, date],
    pre_ledger_losses: Decimal,
    arrival: date | None,
) -> list[FigPattern]:
    """Rank every claim subset of ``window`` by total liability across it.

    For each subset the loss chain is rebuilt with exactly those years
    claimed, so a year's disallowed foreign losses (and the knock-on to
    later years' carried losses) are reflected. Returns the patterns
    sorted cheapest-first, with ties broken toward fewer claimed years.
    """

    patterns: list[FigPattern] = []
    for subset in _subsets(window):
        claimed = frozenset(subset)
        chain = loss_carryforward_chain(
            history_rows,
            through_year=window[-1],
            aea_by_year=aea_by_year,
            rate_change_dates=rate_change_dates,
            pre_ledger_losses=pre_ledger_losses,
            arrival=arrival,
            fig_claim_years=claimed,
        )
        per_year: dict[str, Decimal] = {}
        for y in window:
            inp = year_inputs[y]
            allowance = chain[y]
            liab = compute_liability(
                tax_year=y,
                other_income=inp.other_income,
                other_taxable_income=inp.uk_other,
                foreign_other_income=inp.foreign_other,
                interest_income=inp.interest_income,
                interest_wht=inp.interest_wht,
                dividend_income=inp.dividend_income,
                dividend_wht=inp.dividend_wht,
                cgt_taxable_pre=allowance.taxable_pre,
                cgt_taxable_post=allowance.taxable_post,
                bands=inp.bands,
                cgt_rates=inp.cgt_rates,
                fig_claimed=y in claimed,
            )
            per_year[y] = liab.total_liability
        patterns.append(
            FigPattern(
                claimed=claimed,
                total_liability=sum(per_year.values(), Decimal(0)),
                per_year=per_year,
            )
        )

    patterns.sort(key=lambda p: (p.total_liability, len(p.claimed)))
    return patterns
