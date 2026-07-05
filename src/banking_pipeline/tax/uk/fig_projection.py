"""FIG-window projection: the cost of deferring vs. crystallising gains.

A holder inside the 4-year Foreign Income & Gains (FIG) window can realise a
foreign holding's gain *while a claim relieves it to nil*, resetting the base
cost upward — so the currently embedded gain escapes CGT on any eventual
post-window disposal. That opportunity **expires** when the window closes.

This module prices that opportunity. For the sum of the **positive** foreign
unrealised gains (the winners you would crystallise — a foreign *loss* is
disallowed under a FIG claim, so it carries no benefit), it computes the CGT
that would fall due if the gain were instead deferred to a taxable post-window
disposal, by stacking it through the CGT bands at the holder's assumed income
(reusing :func:`compute_liability`). That CGT **is** the saving from
crystallising now, and the act-by date is the end of the last claimable window
year.

Modelling decisions (see the plan): the deferred gain is priced by ``--income``
band-stacking (not a flat rate); it is an **upper bound** — the saving is only
real if the holding is actually sold in the holder's lifetime (CGT is uplifted
to market on death), and the figure ignores the post-window year's annual exempt
amount; growth after today is scenario-neutral and not modelled. Planning aid,
not tax advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.liability import compute_liability
from banking_pipeline.tax.uk.rates import CgtRateSchedule, IncomeTaxBands

_ZERO = Decimal(0)


@dataclass(frozen=True)
class FigProjectionHolding:
    """One foreign holding's unrealised gain/loss (GBP, may be negative)."""

    key: str
    name: str
    unrealised_gbp: Decimal


@dataclass(frozen=True)
class FigProjection:
    """The window projection: what crystallising the foreign winners saves.

    ``crystallisable_gain_gbp`` is the sum of the *positive* foreign unrealised
    gains (the winners); ``net_foreign_unrealised_gbp`` is the net of all
    foreign holdings (winners and losers) for context. ``deferred_cgt_gbp`` is
    the CGT the crystallisable gain would incur on a post-window disposal — the
    saving from crystallising in-window. ``act_by`` is the end of the last
    claimable window year (5 April), or ``None`` when the window has closed.
    """

    window: list[str]  # remaining claimable eligible tax years, ascending
    act_by: date | None
    crystallisable_gain_gbp: Decimal
    net_foreign_unrealised_gbp: Decimal
    deferred_cgt_gbp: Decimal
    holdings: list[FigProjectionHolding]  # foreign holdings, by gain desc
    income_gbp: Decimal
    rate_year: str  # the tax year whose bands/rates priced the gain


def project_fig_window(
    *,
    window: list[str],
    act_by: date | None,
    holdings: list[FigProjectionHolding],
    income: Decimal,
    rate_year: str,
    bands: IncomeTaxBands,
    cgt_rates: CgtRateSchedule,
) -> FigProjection:
    """Price the crystallise-now saving for the remaining FIG window.

    ``window`` / ``act_by`` are resolved by the caller (the remaining eligible
    years whose end is not yet past, and the last one's 5 April). ``holdings``
    are the foreign holdings' unrealised gains. ``income`` sets the marginal
    band the deferred gain stacks on; ``bands`` / ``cgt_rates`` are the schedule
    for ``rate_year`` (a proxy for the future disposal year). Pure — no clock.
    """

    crystallisable = sum(
        (h.unrealised_gbp for h in holdings if h.unrealised_gbp > _ZERO), _ZERO
    )
    net = sum((h.unrealised_gbp for h in holdings), _ZERO)

    # CGT on the crystallisable gain if instead deferred to a taxable
    # post-window disposal: stack it above the assumed income at the CGT rates.
    # Ignores the AEA (upper-bound framing) and any mid-year rate split (a
    # forward year has none — the whole gain rides the single schedule).
    deferred_cgt = compute_liability(
        tax_year=rate_year,
        other_income=income,
        other_taxable_income=_ZERO,
        interest_income=_ZERO,
        interest_wht=_ZERO,
        dividend_income=_ZERO,
        dividend_wht=_ZERO,
        cgt_taxable_pre=crystallisable,
        cgt_taxable_post=_ZERO,
        bands=bands,
        cgt_rates=cgt_rates,
    ).cgt_tax

    return FigProjection(
        window=window,
        act_by=act_by,
        crystallisable_gain_gbp=crystallisable,
        net_foreign_unrealised_gbp=net,
        deferred_cgt_gbp=deferred_cgt,
        holdings=sorted(
            holdings, key=lambda h: h.unrealised_gbp, reverse=True
        ),
        income_gbp=income,
        rate_year=rate_year,
    )
