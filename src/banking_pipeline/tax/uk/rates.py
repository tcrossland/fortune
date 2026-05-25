"""Statutory UK income-tax and CGT rates / bands, by tax year.

These tables feed the current-year tax-liability forecast
(:mod:`banking_pipeline.tax.uk.liability`) — the only consumer that
turns the SA108/SA106 *amounts* into a pound figure. The CGT *annual
exempt amount* and the mid-year *rate-change dates* live in
:class:`banking_pipeline.config.Settings` (they predate this module and
the loss-carry-forward chain reads them); the rate *percentages* and the
income-tax bands live here. ``Settings`` exposes both registries as
overridable fields, so this module is the statutory default, not the
single source.

England/Wales/NI rates only — Scottish income-tax bands differ and are
out of scope. Values are frozen across 2024-25..2026-27 except the CGT
percentages, which stepped up part-way through 2024-25 (hence the
pre/post split keyed to ``cgt_rate_change_dates``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class IncomeTaxBands:
    """Income-tax thresholds, rates and 0%-allowances for one tax year.

    Thresholds are expressed two ways on purpose: ``basic_band`` and
    ``additional_threshold`` are *income* figures (matching how HMRC
    publishes them), and the liability engine converts them to
    taxable-income terms by subtracting the (possibly tapered) personal
    allowance. ``starting_savings_band`` / ``psa_*`` / ``dividend_allowance``
    are 0%-rate sub-bands that still occupy band space.
    """

    personal_allowance: Decimal
    # Adjusted-net-income at which the personal allowance starts tapering
    # (£1 lost per £2 over), fully gone by ``+ 2 * personal_allowance``.
    pa_taper_threshold: Decimal
    # Width of the basic-rate band measured in taxable income (income
    # above the personal allowance). The higher rate starts here.
    basic_band: Decimal
    # Income (not taxable income) at which the additional rate begins.
    additional_threshold: Decimal

    basic_rate: Decimal
    higher_rate: Decimal
    additional_rate: Decimal

    # Personal savings allowance (0%-rate band for interest): the basic
    # figure for a basic-rate taxpayer, the higher figure for a
    # higher-rate taxpayer, nil for an additional-rate taxpayer.
    psa_basic: Decimal
    psa_higher: Decimal
    # Starting-rate band for savings (0%), reduced £1-for-£1 by
    # non-savings income above the personal allowance.
    starting_savings_band: Decimal

    dividend_allowance: Decimal
    dividend_basic_rate: Decimal
    dividend_higher_rate: Decimal
    dividend_additional_rate: Decimal


@dataclass(frozen=True)
class CgtRates:
    """Lower/higher CGT rates for non-residential assets (shares/funds)."""

    lower: Decimal
    higher: Decimal


@dataclass(frozen=True)
class CgtRateSchedule:
    """CGT rates split pre / on-or-after the year's rate-change date.

    When a year has no mid-year change the two are identical, so the
    forecast can read ``post`` (or ``pre``) without branching.
    """

    pre: CgtRates
    post: CgtRates


# 2024-25 income-tax bands; frozen, so 2025-26 and 2026-27 reuse them.
_FROZEN_BANDS = IncomeTaxBands(
    personal_allowance=Decimal("12570"),
    pa_taper_threshold=Decimal("100000"),
    basic_band=Decimal("37700"),
    additional_threshold=Decimal("125140"),
    basic_rate=Decimal("0.20"),
    higher_rate=Decimal("0.40"),
    additional_rate=Decimal("0.45"),
    psa_basic=Decimal("1000"),
    psa_higher=Decimal("500"),
    starting_savings_band=Decimal("5000"),
    dividend_allowance=Decimal("500"),
    dividend_basic_rate=Decimal("0.0875"),
    dividend_higher_rate=Decimal("0.3375"),
    dividend_additional_rate=Decimal("0.3935"),
)


def default_income_bands() -> dict[str, IncomeTaxBands]:
    return {
        "2024-25": _FROZEN_BANDS,
        "2025-26": _FROZEN_BANDS,
        "2026-27": _FROZEN_BANDS,
    }


# CGT rates for shares/funds. 2024-25 stepped from 10/20 to 18/24 on
# 30 Oct 2024 (the ``cgt_rate_change_dates`` boundary); 2025-26 onward is
# 18/24 for the whole year.
_PRE_OCT24 = CgtRates(lower=Decimal("0.10"), higher=Decimal("0.20"))
_FROM_OCT24 = CgtRates(lower=Decimal("0.18"), higher=Decimal("0.24"))


def default_cgt_rates() -> dict[str, CgtRateSchedule]:
    return {
        "2024-25": CgtRateSchedule(pre=_PRE_OCT24, post=_FROM_OCT24),
        "2025-26": CgtRateSchedule(pre=_FROM_OCT24, post=_FROM_OCT24),
        "2026-27": CgtRateSchedule(pre=_FROM_OCT24, post=_FROM_OCT24),
    }
