"""UK CGT annual exempt amount + loss carry-forward.

Section 104 matching (``sa108.match_history``) produces a gain or loss per
disposal portion. This module layers the year-level allowances on top:
the annual exempt amount (AEA) and the running pool of allowable losses
carried between tax years. It encodes HMRC's deduction ordering, which is
not free choice:

1. **Current-year losses** are set against current-year gains in full —
   even when that wastes the AEA. Any surplus is carried forward.
2. **Brought-forward losses** are then used, but *only down to the AEA*:
   you never waste carried losses against the exempt amount, so only
   ``net_gain - AEA`` of them is consumed.
3. **The AEA** is deducted last.

Where a tax year has a mid-year CGT rate change (e.g. 30 Oct 2024 for
2024-25), gains on/after the change are taxed at the higher rate. Losses
and the AEA are fungible across the split, so we absorb them against the
higher-rate (``post``) gains first, leaving any taxable remainder in the
lower-rate (``pre``) bucket — the allocation that minimises the bill. The
*total* taxable gain is unaffected by allocation; only its rate-bucket
placement is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.sa108 import Sa108Row
from banking_pipeline.tax.uk.tax_year import date_to_tax_year

# Reporting statuses whose disposals are CGT (SA108) and therefore draw on
# the AEA and the allowable-loss pool. Non-reporting (offshore income
# gains) and unclassified holdings are taxed as income / flagged, not CGT.
CGT_STATUSES = frozenset({"reporting", "uk-domestic"})

_ZERO = Decimal(0)


@dataclass(frozen=True)
class CgtAllowanceResult:
    """The allowance outcome for one tax year, after the statutory order."""

    tax_year: str
    # Whether this year has a mid-year rate change (so pre/post is
    # meaningful); when False everything sits in the ``pre`` bucket.
    rate_split: bool
    # CGT-eligible gains by rate bucket and total current-year losses
    # (magnitude), before any deduction.
    gains_pre: Decimal
    gains_post: Decimal
    current_year_losses: Decimal
    # Net gain after current-year losses, and the loss surplus carried on.
    net_gain: Decimal
    current_year_loss_carried: Decimal
    # Brought-forward loss pool available this year and the amount used.
    brought_forward_available: Decimal
    brought_forward_used: Decimal
    # AEA available and used.
    annual_exempt_amount: Decimal
    annual_exempt_used: Decimal
    # Taxable gain left in each rate bucket after all deductions.
    taxable_pre: Decimal
    taxable_post: Decimal
    # Total allowable losses carried into the next tax year.
    losses_carried_forward: Decimal

    @property
    def taxable_total(self) -> Decimal:
        return self.taxable_pre + self.taxable_post


def apply_cgt_allowances(
    *,
    tax_year: str,
    gains_pre: Decimal,
    gains_post: Decimal,
    current_year_losses: Decimal,
    brought_forward: Decimal,
    annual_exempt_amount: Decimal,
    rate_split: bool,
) -> CgtAllowanceResult:
    """Apply the CGT deduction ordering to one year's gains and losses.

    ``gains_pre`` / ``gains_post`` are the (non-negative) gain totals in
    the lower- and higher-rate buckets; ``current_year_losses`` is the
    magnitude of that year's losses (fungible across buckets);
    ``brought_forward`` is the allowable-loss pool entering the year.
    Losses and the AEA are absorbed against ``gains_post`` first.
    """

    # 1. Current-year losses against current-year gains, post bucket first.
    g_post = max(_ZERO, gains_post - current_year_losses)
    loss_after_post = max(_ZERO, current_year_losses - gains_post)
    g_pre = max(_ZERO, gains_pre - loss_after_post)
    cy_used = (gains_post - g_post) + (gains_pre - g_pre)
    cy_carried = current_year_losses - cy_used
    net_gain = g_post + g_pre

    # 2. Brought-forward losses, but only enough to reach the AEA.
    bf_used = min(brought_forward, max(_ZERO, net_gain - annual_exempt_amount))
    b_post = max(_ZERO, g_post - bf_used)
    bf_after_post = max(_ZERO, bf_used - g_post)
    b_pre = max(_ZERO, g_pre - bf_after_post)

    # 3. The AEA, again post bucket first.
    aea_used = min(annual_exempt_amount, b_post + b_pre)
    t_post = max(_ZERO, b_post - annual_exempt_amount)
    aea_after_post = max(_ZERO, annual_exempt_amount - b_post)
    t_pre = max(_ZERO, b_pre - aea_after_post)

    losses_cf = (brought_forward - bf_used) + cy_carried

    return CgtAllowanceResult(
        tax_year=tax_year,
        rate_split=rate_split,
        gains_pre=gains_pre,
        gains_post=gains_post,
        current_year_losses=current_year_losses,
        net_gain=net_gain,
        current_year_loss_carried=cy_carried,
        brought_forward_available=brought_forward,
        brought_forward_used=bf_used,
        annual_exempt_amount=annual_exempt_amount,
        annual_exempt_used=aea_used,
        taxable_pre=t_pre,
        taxable_post=t_post,
        losses_carried_forward=losses_cf,
    )


def _next_year_label(label: str) -> str:
    """``"2023-24"`` → ``"2024-25"``."""

    start = int(label[:4]) + 1
    return f"{start}-{(start + 1) % 100:02d}"


def loss_carryforward_chain(
    rows: list[Sa108Row],
    *,
    through_year: str,
    aea_by_year: dict[str, Decimal],
    rate_change_dates: dict[str, date],
    pre_ledger_losses: Decimal = _ZERO,
) -> dict[str, CgtAllowanceResult]:
    """Thread allowable losses forward across tax years.

    ``rows`` is the full-history set of matched disposals (period unset);
    only CGT-eligible statuses participate. The chain runs from the
    earliest year with a disposal through ``through_year`` inclusive
    (materialising empty years so a year with only carried losses still
    appears), seeding the loss pool with ``pre_ledger_losses`` in the
    first year. Returns one :class:`CgtAllowanceResult` per year, keyed by
    label.
    """

    cgt_rows = [r for r in rows if r.reporting_status in CGT_STATUSES]
    by_year: dict[str, list[Sa108Row]] = {}
    for r in cgt_rows:
        by_year.setdefault(date_to_tax_year(r.disposal_date), []).append(r)

    start_label = min(by_year) if by_year else through_year
    if int(through_year[:4]) < int(start_label[:4]):
        start_label = through_year

    results: dict[str, CgtAllowanceResult] = {}
    carried = pre_ledger_losses
    label = start_label
    while True:
        year_rows = by_year.get(label, [])
        rcd = rate_change_dates.get(label)
        rate_split = rcd is not None
        gains_pre = _ZERO
        gains_post = _ZERO
        losses = _ZERO
        for r in year_rows:
            if r.gain_gbp < 0:
                losses += -r.gain_gbp
            elif rate_split and rcd is not None and r.disposal_date >= rcd:
                gains_post += r.gain_gbp
            else:
                gains_pre += r.gain_gbp

        result = apply_cgt_allowances(
            tax_year=label,
            gains_pre=gains_pre,
            gains_post=gains_post,
            current_year_losses=losses,
            brought_forward=carried,
            annual_exempt_amount=aea_by_year.get(label, _ZERO),
            rate_split=rate_split,
        )
        results[label] = result
        carried = result.losses_carried_forward
        if label == through_year:
            break
        label = _next_year_label(label)

    return results
