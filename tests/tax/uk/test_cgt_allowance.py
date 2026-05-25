"""CGT annual exempt amount + loss carry-forward."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.cgt_allowance import (
    apply_cgt_allowances,
    loss_carryforward_chain,
)
from banking_pipeline.tax.uk.sa108 import Sa108Row

D = Decimal


def _row(*, gain: Decimal, on: date, isin: str = "X", status: str = "reporting") -> Sa108Row:
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
    )


# --- apply_cgt_allowances ---------------------------------------------------


def test_gain_below_aea_is_fully_exempt_and_preserves_bf() -> None:
    r = apply_cgt_allowances(
        tax_year="2025-26",
        gains_pre=D(2000),
        gains_post=D(0),
        current_year_losses=D(0),
        brought_forward=D(5000),
        annual_exempt_amount=D(3000),
        rate_split=False,
    )
    assert r.taxable_total == D(0)
    # Net gain is under the AEA, so no brought-forward loss is consumed.
    assert r.brought_forward_used == D(0)
    assert r.losses_carried_forward == D(5000)
    assert r.annual_exempt_used == D(2000)


def test_bf_losses_used_only_down_to_aea() -> None:
    # Gain 10k, AEA 3k → only 7k of b/f losses are used, not all 12k.
    r = apply_cgt_allowances(
        tax_year="2025-26",
        gains_pre=D(10000),
        gains_post=D(0),
        current_year_losses=D(0),
        brought_forward=D(12000),
        annual_exempt_amount=D(3000),
        rate_split=False,
    )
    assert r.brought_forward_used == D(7000)
    assert r.taxable_total == D(0)
    assert r.losses_carried_forward == D(5000)


def test_current_year_losses_offset_first_even_wasting_aea() -> None:
    # 4k gain, 4k current-year loss → net 0, AEA wasted, nothing taxable,
    # b/f untouched.
    r = apply_cgt_allowances(
        tax_year="2025-26",
        gains_pre=D(4000),
        gains_post=D(0),
        current_year_losses=D(4000),
        brought_forward=D(1000),
        annual_exempt_amount=D(3000),
        rate_split=False,
    )
    assert r.net_gain == D(0)
    assert r.taxable_total == D(0)
    assert r.brought_forward_used == D(0)
    assert r.losses_carried_forward == D(1000)


def test_current_year_loss_surplus_carried_forward() -> None:
    r = apply_cgt_allowances(
        tax_year="2025-26",
        gains_pre=D(1000),
        gains_post=D(0),
        current_year_losses=D(5000),
        brought_forward=D(2000),
        annual_exempt_amount=D(3000),
        rate_split=False,
    )
    # 5k loss − 1k gain = 4k surplus, added to the 2k brought forward.
    assert r.current_year_loss_carried == D(4000)
    assert r.losses_carried_forward == D(6000)
    assert r.taxable_total == D(0)


def test_taxable_gain_after_bf_and_aea() -> None:
    r = apply_cgt_allowances(
        tax_year="2025-26",
        gains_pre=D(20000),
        gains_post=D(0),
        current_year_losses=D(0),
        brought_forward=D(5000),
        annual_exempt_amount=D(3000),
        rate_split=False,
    )
    # 20k − 5k bf − 3k AEA = 12k taxable.
    assert r.taxable_total == D(12000)
    assert r.losses_carried_forward == D(0)


def test_rate_split_allocates_relief_to_post_bucket_first() -> None:
    # Higher-rate (post) gains absorb losses + AEA first, leaving the
    # taxable remainder in the lower-rate (pre) bucket.
    r = apply_cgt_allowances(
        tax_year="2024-25",
        gains_pre=D(10000),
        gains_post=D(4000),
        current_year_losses=D(2000),
        brought_forward=D(0),
        annual_exempt_amount=D(3000),
        rate_split=True,
    )
    # CY loss 2k against post (4k → 2k); AEA 3k against post (2k → 0,
    # 1k spills to pre); pre 10k − 1k = 9k taxable. Post fully relieved.
    assert r.taxable_post == D(0)
    assert r.taxable_pre == D(9000)
    assert r.taxable_total == D(9000)


# --- loss_carryforward_chain ------------------------------------------------


def test_chain_threads_losses_across_years() -> None:
    rows = [
        _row(gain=D(-4000), on=date(2023, 6, 1)),   # 2023-24 loss
        _row(gain=D(10000), on=date(2024, 6, 1)),   # 2024-25 gain
    ]
    chain = loss_carryforward_chain(
        rows,
        through_year="2024-25",
        aea_by_year={"2023-24": D(6000), "2024-25": D(3000)},
        rate_change_dates={},
        pre_ledger_losses=D(0),
    )
    # Loss year carries 4k forward; gain year uses it down to the AEA.
    assert chain["2023-24"].losses_carried_forward == D(4000)
    assert chain["2024-25"].brought_forward_used == D(4000)
    assert chain["2024-25"].taxable_total == D(3000)
    assert chain["2024-25"].losses_carried_forward == D(0)


def test_chain_seeds_pre_ledger_losses() -> None:
    rows = [_row(gain=D(10000), on=date(2025, 6, 1))]
    chain = loss_carryforward_chain(
        rows,
        through_year="2025-26",
        aea_by_year={"2025-26": D(3000)},
        rate_change_dates={},
        pre_ledger_losses=D(5000),
    )
    assert chain["2025-26"].brought_forward_available == D(5000)
    # Gain 10k, AEA 3k → 7k of relief wanted but only 5k available, all used.
    assert chain["2025-26"].brought_forward_used == D(5000)
    assert chain["2025-26"].taxable_total == D(2000)


def test_chain_materialises_requested_year_with_no_disposals() -> None:
    rows = [_row(gain=D(-2000), on=date(2023, 6, 1))]
    chain = loss_carryforward_chain(
        rows,
        through_year="2025-26",
        aea_by_year={"2023-24": D(6000), "2024-25": D(3000), "2025-26": D(3000)},
        rate_change_dates={},
        pre_ledger_losses=D(0),
    )
    # The empty later years still appear and carry the loss forward.
    assert set(chain) == {"2023-24", "2024-25", "2025-26"}
    assert chain["2025-26"].brought_forward_available == D(2000)
    assert chain["2025-26"].losses_carried_forward == D(2000)


def test_chain_ignores_non_cgt_statuses() -> None:
    rows = [
        _row(gain=D(10000), on=date(2025, 6, 1), status="non-reporting"),
        _row(gain=D(4000), on=date(2025, 6, 1), status="reporting"),
    ]
    chain = loss_carryforward_chain(
        rows,
        through_year="2025-26",
        aea_by_year={"2025-26": D(3000)},
        rate_change_dates={},
        pre_ledger_losses=D(0),
    )
    # Only the 4k reporting gain counts; the offshore gain is not CGT.
    assert chain["2025-26"].net_gain == D(4000)
    assert chain["2025-26"].taxable_total == D(1000)
