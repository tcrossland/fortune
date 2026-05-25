"""UK tax-liability estimate from the SA108/SA106 amounts.

The SA108/SA106 machinery produces taxable *amounts* (capital gains net
of the AEA and losses, foreign dividends, foreign interest, offshore
income gains, deeply discounted income). This module turns them into a
single estimated pound figure for the ``tax-forecast`` command, applying
the UK stacking order:

1. **Non-savings income** — the taxpayer's expected income for the year
   plus offshore income gains and deeply discounted securities profits
   (both charged to income tax, not CGT/dividends).
2. **Savings income** — foreign interest, after the starting-rate band
   and the personal savings allowance.
3. **Dividend income** — foreign dividends, after the dividend allowance.
4. **Capital gains** — stacked on top of taxable income; the basic-rate
   band left after income is taxed at the lower CGT rate, the rest at the
   higher rate.

Foreign withholding tax is credited against the UK tax on that same
income (foreign tax credit relief), capped at the UK liability on it.

This is a forecast, not a return: it assumes England/Wales/NI rates, a
single taxpayer, and the marginal band implied by the supplied expected
income. It does not model gift aid, pension relief, the marriage
allowance, or Scottish bands. The personal-allowance taper over £100k is
applied; finer interactions (e.g. the savings starting-rate-band shading)
are approximated and documented inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from banking_pipeline.tax.uk.rates import CgtRateSchedule, IncomeTaxBands

_ZERO = Decimal(0)
_TWO = Decimal(2)


@dataclass(frozen=True)
class LiabilityResult:
    """The estimated UK liability and the breakdown behind it."""

    tax_year: str

    # Whether a 4-year FIG claim was applied (foreign income relieved,
    # personal allowance forfeited) and the foreign income so relieved.
    fig_claimed: bool
    relieved_income: Decimal

    # --- inputs / allowances ---
    other_income: Decimal
    other_taxable_income: Decimal  # UK-situs income-charged gains (taxed)
    personal_allowance: Decimal  # after the £100k taper (0 if FIG claimed)

    # --- non-savings income tax ---
    nonsavings_taxable: Decimal
    nonsavings_tax: Decimal

    # --- savings (interest) ---
    interest_income: Decimal
    starting_rate_used: Decimal
    psa_used: Decimal
    interest_taxable: Decimal
    interest_tax: Decimal
    interest_wht: Decimal
    interest_ftcr: Decimal

    # --- dividends ---
    dividend_income: Decimal
    dividend_allowance_used: Decimal
    dividend_taxable: Decimal
    dividend_tax: Decimal
    dividend_wht: Decimal
    dividend_ftcr: Decimal

    # --- income-tax totals ---
    income_tax_before_ftcr: Decimal
    foreign_tax_credit: Decimal
    income_tax: Decimal

    # --- capital gains tax ---
    cgt_taxable_pre: Decimal
    cgt_taxable_post: Decimal
    cgt_basic_band_remaining: Decimal
    cgt_at_lower: Decimal
    cgt_at_higher: Decimal
    cgt_tax: Decimal

    total_liability: Decimal


def _tax_tranche(
    amount: Decimal,
    used: Decimal,
    *,
    bands: IncomeTaxBands,
    personal_allowance: Decimal,
    dividend: bool,
) -> tuple[Decimal, Decimal]:
    """Tax ``amount`` of income sitting on top of ``used`` taxable income.

    Bands are measured in taxable income (i.e. after the personal
    allowance). The higher-rate band tops out where the additional rate
    begins, which in taxable-income terms is ``additional_threshold`` less
    the (possibly tapered) personal allowance. Returns
    ``(tax, new_cursor)``.
    """

    basic_limit = bands.basic_band
    higher_limit = bands.additional_threshold - personal_allowance
    segments = (
        (
            basic_limit,
            bands.dividend_basic_rate if dividend else bands.basic_rate,
        ),
        (
            higher_limit,
            bands.dividend_higher_rate if dividend else bands.higher_rate,
        ),
        (
            None,
            bands.dividend_additional_rate if dividend else bands.additional_rate,
        ),
    )
    tax = _ZERO
    remaining = amount
    cursor = used
    for top, rate in segments:
        if remaining <= _ZERO:
            break
        room = remaining if top is None else max(_ZERO, top - cursor)
        take = min(remaining, room)
        tax += take * rate
        cursor += take
        remaining -= take
    return tax, cursor


def _marginal_psa(total_income: Decimal, bands: IncomeTaxBands) -> Decimal:
    """Personal savings allowance for the band the taxpayer lands in:
    £1,000 basic, £500 higher, nil additional."""

    basic_income_limit = bands.personal_allowance + bands.basic_band
    if total_income <= basic_income_limit:
        return bands.psa_basic
    if total_income <= bands.additional_threshold:
        return bands.psa_higher
    return _ZERO


def compute_liability(
    *,
    tax_year: str,
    other_income: Decimal,
    other_taxable_income: Decimal,
    foreign_other_income: Decimal = _ZERO,
    interest_income: Decimal,
    interest_wht: Decimal,
    dividend_income: Decimal,
    dividend_wht: Decimal,
    cgt_taxable_pre: Decimal,
    cgt_taxable_post: Decimal,
    bands: IncomeTaxBands,
    cgt_rates: CgtRateSchedule,
    fig_claimed: bool = False,
) -> LiabilityResult:
    """Estimate the UK liability for one tax year from its taxable amounts.

    ``other_income`` is the taxpayer's expected non-savings, non-dividend
    taxable income (e.g. salary + rent) *before* the personal allowance;
    ``other_taxable_income`` is UK-situs income-charged investment profit
    (offshore income gains + deeply discounted securities that aren't
    relievable); ``foreign_other_income`` is the foreign-situs equivalent.
    ``cgt_taxable_pre`` / ``cgt_taxable_post`` are the CGT figures already
    net of the AEA and losses (and already FIG-adjusted by the chain),
    split by the year's rate-change date.

    When ``fig_claimed`` is true the foreign income (interest, dividends,
    ``foreign_other_income``) is relieved to nil and the personal
    allowance is forfeited — the cost of the claim.
    """

    # A FIG claim relieves foreign income (it drops out of the taxable
    # stacks) and forfeits the personal allowance.
    eff_interest = _ZERO if fig_claimed else interest_income
    eff_dividend = _ZERO if fig_claimed else dividend_income
    eff_foreign_other = _ZERO if fig_claimed else foreign_other_income
    relieved_income = (
        interest_income + dividend_income + foreign_other_income
        if fig_claimed
        else _ZERO
    )

    total_income = (
        other_income + other_taxable_income + eff_foreign_other
        + eff_interest + eff_dividend
    )

    # Personal allowance, tapered £1-for-£2 over £100k, gone by £125,140;
    # forfeited entirely under a FIG claim.
    taper = max(_ZERO, total_income - bands.pa_taper_threshold) / _TWO
    pa = _ZERO if fig_claimed else max(_ZERO, bands.personal_allowance - taper)

    # 1. Non-savings income (expected income + income-charged gains).
    nonsavings = other_income + other_taxable_income + eff_foreign_other
    pa_used_ns = min(pa, nonsavings)
    ns_taxable = nonsavings - pa_used_ns
    pa_left = pa - pa_used_ns
    ns_tax, used = _tax_tranche(
        ns_taxable, _ZERO, bands=bands, personal_allowance=pa, dividend=False
    )

    # 2. Savings income (foreign interest; nil if relieved under FIG).
    pa_used_int = min(pa_left, eff_interest)
    int_after_pa = eff_interest - pa_used_int
    pa_left -= pa_used_int
    # Starting-rate band: reduced £1-for-£1 by non-savings taxable income.
    ssr_band = max(_ZERO, bands.starting_savings_band - ns_taxable)
    ssr_used = min(int_after_pa, ssr_band)
    used += ssr_used  # 0%-rate, but it occupies band space
    int_after_ssr = int_after_pa - ssr_used
    psa = _marginal_psa(total_income, bands)
    psa_used = min(int_after_ssr, psa)
    used += psa_used  # 0%-rate, occupies band space
    int_taxable = int_after_ssr - psa_used
    int_tax, used = _tax_tranche(
        int_taxable, used, bands=bands, personal_allowance=pa, dividend=False
    )

    # 3. Dividend income (foreign dividends; nil if relieved under FIG).
    pa_used_div = min(pa_left, eff_dividend)
    div_after_pa = eff_dividend - pa_used_div
    da_used = min(div_after_pa, bands.dividend_allowance)
    used += da_used  # 0%-rate, occupies band space
    div_taxable = div_after_pa - da_used
    div_tax, used = _tax_tranche(
        div_taxable, used, bands=bands, personal_allowance=pa, dividend=True
    )

    # Foreign tax credit relief: WHT credited against UK tax on that
    # income, capped at the UK liability on it.
    int_ftcr = min(interest_wht, int_tax)
    div_ftcr = min(dividend_wht, div_tax)
    income_tax_before_ftcr = ns_tax + int_tax + div_tax
    ftcr = int_ftcr + div_ftcr
    income_tax = income_tax_before_ftcr - ftcr

    # 4. Capital gains, stacked on top of taxable income. The basic-rate
    # band left after income is taxed at the lower CGT rate. With a
    # mid-year rate change the basic band is fungible across the pre/post
    # buckets, so allocate it where it saves the most — the bucket with
    # the larger (higher − lower) spread first.
    band_left = max(_ZERO, bands.basic_band - used)
    pre_spread = cgt_rates.pre.higher - cgt_rates.pre.lower
    post_spread = cgt_rates.post.higher - cgt_rates.post.lower
    buckets = [
        (cgt_taxable_pre, cgt_rates.pre),
        (cgt_taxable_post, cgt_rates.post),
    ]
    if post_spread > pre_spread:
        buckets.reverse()

    cgt_tax = _ZERO
    cgt_lower = _ZERO
    cgt_higher = _ZERO
    band = band_left
    for gain, rates in buckets:
        at_lower = min(gain, band)
        at_higher = gain - at_lower
        band -= at_lower
        cgt_lower += at_lower
        cgt_higher += at_higher
        cgt_tax += at_lower * rates.lower + at_higher * rates.higher

    return LiabilityResult(
        tax_year=tax_year,
        fig_claimed=fig_claimed,
        relieved_income=relieved_income,
        other_income=other_income,
        other_taxable_income=other_taxable_income,
        personal_allowance=pa,
        nonsavings_taxable=ns_taxable,
        nonsavings_tax=ns_tax,
        interest_income=interest_income,
        starting_rate_used=ssr_used,
        psa_used=psa_used,
        interest_taxable=int_taxable,
        interest_tax=int_tax,
        interest_wht=interest_wht,
        interest_ftcr=int_ftcr,
        dividend_income=dividend_income,
        dividend_allowance_used=da_used,
        dividend_taxable=div_taxable,
        dividend_tax=div_tax,
        dividend_wht=dividend_wht,
        dividend_ftcr=div_ftcr,
        income_tax_before_ftcr=income_tax_before_ftcr,
        foreign_tax_credit=ftcr,
        income_tax=income_tax,
        cgt_taxable_pre=cgt_taxable_pre,
        cgt_taxable_post=cgt_taxable_post,
        cgt_basic_band_remaining=band_left,
        cgt_at_lower=cgt_lower,
        cgt_at_higher=cgt_higher,
        cgt_tax=cgt_tax,
        total_liability=income_tax + cgt_tax,
    )
