"""UK residence + 4-year Foreign Income & Gains (FIG) regime helpers.

The rest of the tax pipeline assumes UK arising-basis residence across
the whole history. These helpers introduce two corrections, both driven
by config (``uk_residence_start_date`` / ``fig_claim_years``):

1. **Pre-residence.** While non-UK resident, foreign income and gains are
   not UK-taxable. An event *before* the arrival date sits in the
   non-resident (overseas) part of a split year and drops out; a tax year
   ending before arrival drops out entirely.
2. **FIG claim.** For up to the first four UK-resident tax years (and no
   earlier than 2025-26, when the regime began), the user may elect to
   relieve foreign income and non-UK gains to nil — at the cost of the
   personal allowance and the CGT annual exempt amount for that year.

Neither the 10-prior-non-resident-years eligibility test nor the split
itself is derivable from the ledger; configuring an arrival date asserts
eligibility. The section 104 pool is unaffected — acquisitions feed it
whenever they happened (including while non-resident); only the taxable
*output* is residence-filtered.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal, NamedTuple

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.tax.uk.tax_year import date_to_tax_year, tax_year_bounds

# The 4-year FIG regime applies from 6 April 2025 (tax year 2025-26).
FIG_REGIME_FIRST_YEAR = "2025-26"
FIG_MAX_YEARS = 4

FigKind = Literal["income", "gain", "loss"]


class FigDesignationRow(NamedTuple):
    """One foreign item relieved under a FIG claim.

    ``kind`` buckets the row so the designation can show relieved income,
    relieved gains, and *disallowed* foreign losses separately rather than
    silently netting a loss into a single total: ``"income"`` (foreign
    dividends, interest, ERI), ``"gain"`` (a non-UK chargeable gain ≥ 0),
    or ``"loss"`` (a non-UK disposal at a loss — its loss relief is
    forfeited by the claim). ``category`` keeps the finer HMRC label
    (e.g. ``"offshore income gain"``); ``amount_gbp`` is signed, so a
    ``"loss"`` row is negative.
    """

    kind: FigKind
    category: str
    country: str
    isin: str
    name: str
    amount_gbp: Decimal


class FigSubtotals(NamedTuple):
    """The FIG designation split three ways plus the net. ``losses`` is
    signed (≤ 0); its magnitude is the loss relief forfeited by the claim.
    ``net = income + gains + losses``."""

    income: Decimal
    gains: Decimal
    losses: Decimal
    net: Decimal


def fig_subtotals(rows: list[FigDesignationRow]) -> FigSubtotals:
    """Bucket the designation rows into relieved income / relieved gains /
    disallowed losses (and their net) by ``kind``."""

    income = sum((r.amount_gbp for r in rows if r.kind == "income"), Decimal(0))
    gains = sum((r.amount_gbp for r in rows if r.kind == "gain"), Decimal(0))
    losses = sum((r.amount_gbp for r in rows if r.kind == "loss"), Decimal(0))
    return FigSubtotals(income, gains, losses, income + gains + losses)


def _next_year_label(label: str) -> str:
    """``"2025-26"`` → ``"2026-27"``."""

    start = int(label[:4]) + 1
    return f"{start}-{(start + 1) % 100:02d}"


def residence_start_year(arrival: date) -> str:
    """The UK tax-year label containing the arrival date (year 1 of
    residence for the FIG window)."""

    return date_to_tax_year(arrival)


def is_pre_residence_year(year_label: str, arrival: date | None) -> bool:
    """True when the whole tax year falls before arrival (wholly
    non-resident → nothing to report). ``None`` arrival → never."""

    if arrival is None:
        return False
    _, end = tax_year_bounds(year_label)
    return end < arrival


def is_pre_residence(on_date: date, arrival: date | None) -> bool:
    """True when an event predates arrival — the non-resident / overseas
    part of a (possibly split) tax year, so not UK-taxable."""

    if arrival is None:
        return False
    return on_date < arrival


def fig_eligible_years(arrival: date | None) -> frozenset[str]:
    """The tax years for which a FIG claim is available.

    Four consecutive tax years from the first year of UK residence, less
    any before the regime's start (2025-26) — so a person resident since
    2023-24 can still claim for the 2025-26 and 2026-27 remainder of their
    four-year window.
    """

    if arrival is None:
        return frozenset()
    label = residence_start_year(arrival)
    window: list[str] = []
    for _ in range(FIG_MAX_YEARS):
        window.append(label)
        label = _next_year_label(label)
    first = int(FIG_REGIME_FIRST_YEAR[:4])
    return frozenset(y for y in window if int(y[:4]) >= first)


def ineligible_claims(
    claims: frozenset[str], arrival: date | None
) -> list[str]:
    """Claimed years that aren't within the eligible FIG window (for a
    warning). Sorted; empty when every claim is valid."""

    eligible = fig_eligible_years(arrival)
    return sorted(claims - eligible)


def gain_is_foreign(meta: CommodityMetadata | None) -> bool:
    """Whether a disposal's gain is relievable under a FIG claim.

    A non-UK-situs asset's gain is foreign (relievable); a UK asset's is
    not. With no metadata the situs can't be asserted, so we treat it as
    UK (no relief) — the safe default, surfaced elsewhere as an
    unclassified-holding warning.
    """

    if meta is None:
        return False
    return not meta.resolved_uk_situs
