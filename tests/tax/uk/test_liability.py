"""UK tax-liability stacking engine."""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.tax.uk.liability import compute_liability
from banking_pipeline.tax.uk.rates import default_cgt_rates, default_income_bands

D = Decimal
BANDS = default_income_bands()["2025-26"]
CGT = default_cgt_rates()["2025-26"]
CGT_2024 = default_cgt_rates()["2024-25"]  # pre 10/20, post 18/24


def _liab(**kw: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = dict(
        tax_year="2025-26",
        other_income=D(0),
        other_taxable_income=D(0),
        interest_income=D(0),
        interest_wht=D(0),
        dividend_income=D(0),
        dividend_wht=D(0),
        cgt_taxable_pre=D(0),
        cgt_taxable_post=D(0),
        bands=BANDS,
        cgt_rates=CGT,
    )
    base.update(kw)
    return compute_liability(**base)  # type: ignore[arg-type]


def test_basic_rate_non_savings_income() -> None:
    # 30,000 − 12,570 PA = 17,430 @ 20% = 3,486.
    r = _liab(other_income=D(30000))
    assert r.personal_allowance == D(12570)
    assert r.nonsavings_taxable == D(17430)
    assert r.nonsavings_tax == D("3486.00")
    assert r.cgt_tax == D(0)
    assert r.total_liability == D("3486.00")


def test_personal_allowance_taper_over_100k() -> None:
    # 120,000 income → PA tapered by (120,000−100,000)/2 = 10,000 → PA 2,570.
    r = _liab(other_income=D(120000))
    assert r.personal_allowance == D(2570)
    # taxable 117,430: 37,700@20% + 79,730@40% = 7,540 + 31,892 = 39,432.
    assert r.nonsavings_taxable == D(117430)
    assert r.total_liability == D("39432.00")


def test_higher_rate_dividends_use_allowance_then_dividend_rate() -> None:
    # 60k salary puts dividends in the higher band; 500 allowance at 0%,
    # the rest at 33.75%.
    r = _liab(other_income=D(60000), dividend_income=D(3000))
    assert r.dividend_allowance_used == D(500)
    assert r.dividend_taxable == D(2500)
    assert r.dividend_tax == D("843.75")  # 2,500 * 0.3375


def test_basic_rate_savings_allowance() -> None:
    # 20k income → basic-rate taxpayer → £1,000 PSA; 1,500 interest leaves
    # 500 taxable @ 20% = 100. Starting-rate band is eroded by the 7,430
    # of non-savings taxable income.
    r = _liab(other_income=D(20000), interest_income=D(1500))
    assert r.starting_rate_used == D(0)
    assert r.psa_used == D(1000)
    assert r.interest_taxable == D(500)
    assert r.interest_tax == D("100.00")


def test_foreign_tax_credit_capped_at_uk_tax() -> None:
    # 500 WHT on a dividend whose UK tax is only 168.75 → credit capped.
    r = _liab(other_income=D(60000), dividend_income=D(1000), dividend_wht=D(500))
    assert r.dividend_tax == D("168.75")
    assert r.dividend_ftcr == D("168.75")
    assert r.foreign_tax_credit == D("168.75")
    # Net dividend tax is fully relieved; only the salary tax remains.
    assert r.income_tax == r.nonsavings_tax


def test_cgt_within_remaining_basic_band_is_lower_rate() -> None:
    # 20k income leaves 30,270 of basic-rate band; a 30,000 gain fits, so
    # it's all at the lower 18% rate.
    r = _liab(other_income=D(20000), cgt_taxable_pre=D(30000))
    assert r.cgt_basic_band_remaining == D(30270)
    assert r.cgt_at_lower == D(30000)
    assert r.cgt_at_higher == D(0)
    assert r.cgt_tax == D("5400.00")  # 30,000 * 0.18


def test_cgt_spills_into_higher_rate_when_band_used() -> None:
    # 60k income exhausts the basic band → the whole gain is at 24%.
    r = _liab(other_income=D(60000), cgt_taxable_pre=D(10000))
    assert r.cgt_basic_band_remaining == D(0)
    assert r.cgt_at_higher == D(10000)
    assert r.cgt_tax == D("2400.00")  # 10,000 * 0.24


def test_mid_year_rate_change_allocates_band_to_wider_spread_first() -> None:
    # 2024-25: pre 10/20 (spread 10), post 18/24 (spread 6). The basic
    # band saves more against pre gains, so it's allocated there first.
    # 44k income → 6,270 of basic band left; pre+post gains 5,000 each.
    r = _liab(
        tax_year="2024-25",
        other_income=D(44000),
        cgt_taxable_pre=D(5000),
        cgt_taxable_post=D(5000),
        cgt_rates=CGT_2024,
    )
    assert r.cgt_basic_band_remaining == D(6270)
    # pre: all 5,000 at lower (10%); post: 1,270 at lower (18%), 3,730 at
    # higher (24%).
    assert r.cgt_at_lower == D(6270)
    assert r.cgt_at_higher == D(3730)
    # 5,000*0.10 + 1,270*0.18 + 3,730*0.24 = 500 + 228.60 + 895.20.
    assert r.cgt_tax == D("1623.80")


def test_offshore_and_deep_discounted_taxed_as_income() -> None:
    # other_taxable_income stacks with non-savings income at marginal rate.
    r = _liab(other_income=D(40000), other_taxable_income=D(5000))
    # 45,000 − 12,570 = 32,430 taxable, all within basic band → 20%.
    assert r.nonsavings_taxable == D(32430)
    assert r.nonsavings_tax == D("6486.00")


def test_fig_claim_relieves_foreign_income_and_forfeits_pa() -> None:
    # Foreign interest, dividends and foreign income-charged gains are all
    # relieved; the personal allowance is forfeited; only salary is taxed.
    r = _liab(
        other_income=D(60000),
        interest_income=D(2000),
        dividend_income=D(3000),
        foreign_other_income=D(5000),
        fig_claimed=True,
    )
    assert r.fig_claimed is True
    assert r.personal_allowance == D(0)
    assert r.relieved_income == D(10000)  # 2,000 + 3,000 + 5,000
    assert r.interest_tax == D(0)
    assert r.dividend_tax == D(0)
    # PA forfeited: whole 60,000 taxed — 37,700@20% + 22,300@40%.
    assert r.nonsavings_taxable == D(60000)
    assert r.nonsavings_tax == D("16460.00")
    assert r.total_liability == D("16460.00")
