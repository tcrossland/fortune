"""Section 104 / same-day / 30-day share matching."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.section_104 import (
    Acquisition,
    Disposal,
    match_disposals,
)


def D(v: str) -> Decimal:  # noqa: N802 — terse Decimal helper for tables
    return Decimal(v)


def test_pure_pool_disposal_uses_weighted_average() -> None:
    acqs = [
        Acquisition(date(2025, 1, 1), D("100"), D("1000")),  # unit 10
        Acquisition(date(2025, 2, 1), D("100"), D("1400")),  # unit 14
    ]
    disps = [Disposal(date(2025, 6, 1), D("100"), D("1500"))]  # unit 15
    [m] = match_disposals(acqs, disps)
    assert m.matched_against == "s104"
    assert m.disposal_qty == D("100")
    # pool avg = (1000+1400)/200 = 12 → cost 1200, gain 300
    assert m.cost_gbp == D("1200.00")
    assert m.gain_gbp == D("300.00")
    assert m.acquisition_dates == []


def test_same_day_match_takes_priority() -> None:
    acqs = [
        Acquisition(date(2025, 1, 1), D("100"), D("1000")),  # pool, untouched
        Acquisition(date(2025, 5, 5), D("50"), D("600")),  # same-day, unit 12
    ]
    disps = [Disposal(date(2025, 5, 5), D("50"), D("700"))]  # unit 14
    [m] = match_disposals(acqs, disps)
    assert m.matched_against == "same-day"
    assert m.cost_gbp == D("600.00")
    assert m.gain_gbp == D("100.00")
    assert m.acquisition_dates == [date(2025, 5, 5)]


def test_bed_and_breakfast_30_day_match() -> None:
    acqs = [Acquisition(date(2025, 5, 31), D("100"), D("1100"))]  # 30 days after
    disps = [Disposal(date(2025, 5, 1), D("100"), D("1500"))]  # unit 15
    [m] = match_disposals(acqs, disps)
    assert m.matched_against == "bed-and-breakfast"
    assert m.cost_gbp == D("1100.00")
    assert m.gain_gbp == D("400.00")
    assert m.acquisition_dates == [date(2025, 5, 31)]


def test_acquisition_just_outside_30_day_window_goes_to_pool() -> None:
    # 31 days after the disposal — outside B&B, so it lands in the pool
    # and the disposal can't match it (pool empty at disposal date).
    acqs = [Acquisition(date(2025, 6, 1), D("100"), D("1100"))]  # 31 days after
    disps = [Disposal(date(2025, 5, 1), D("100"), D("1500"))]
    [m] = match_disposals(acqs, disps)
    assert m.matched_against == "s104"
    assert m.cost_gbp == D("0.00")  # nothing in the pool to match
    assert m.gain_gbp == D("1500.00")


def test_disposal_split_across_all_three_buckets() -> None:
    acqs = [
        Acquisition(date(2025, 1, 1), D("100"), D("1000")),  # pool, unit 10
        Acquisition(date(2025, 6, 20), D("50"), D("600")),  # same-day, unit 12
        Acquisition(date(2025, 6, 30), D("100"), D("1100")),  # B&B (10d), unit 11
    ]
    disps = [Disposal(date(2025, 6, 20), D("250"), D("5000"))]  # unit 20
    records = match_disposals(acqs, disps)
    assert [m.matched_against for m in records] == [
        "same-day",
        "bed-and-breakfast",
        "s104",
    ]
    same_day, bnb, pool = records
    assert (same_day.disposal_qty, same_day.cost_gbp, same_day.gain_gbp) == (
        D("50"),
        D("600.00"),
        D("400.00"),
    )
    assert (bnb.disposal_qty, bnb.cost_gbp, bnb.gain_gbp) == (
        D("100"),
        D("1100.00"),
        D("900.00"),
    )
    assert (pool.disposal_qty, pool.cost_gbp, pool.gain_gbp) == (
        D("100"),
        D("1000.00"),
        D("1000.00"),
    )


def test_multiple_pool_disposals_deplete_in_order() -> None:
    acqs = [Acquisition(date(2025, 1, 1), D("200"), D("2000"))]  # unit 10
    disps = [
        Disposal(date(2025, 3, 1), D("50"), D("750")),  # unit 15
        Disposal(date(2025, 4, 1), D("50"), D("1000")),  # unit 20
    ]
    first, second = match_disposals(acqs, disps)
    assert first.disposal_date == date(2025, 3, 1)
    assert first.gain_gbp == D("250.00")  # 750 - 50*10
    assert second.disposal_date == date(2025, 4, 1)
    assert second.gain_gbp == D("500.00")  # 1000 - 50*10


def test_pool_disposal_can_be_a_loss() -> None:
    acqs = [Acquisition(date(2025, 1, 1), D("100"), D("2000"))]  # unit 20
    disps = [Disposal(date(2025, 6, 1), D("100"), D("1500"))]  # unit 15
    [m] = match_disposals(acqs, disps)
    assert m.gain_gbp == D("-500.00")


def test_records_are_chronological() -> None:
    acqs = [Acquisition(date(2025, 1, 1), D("300"), D("3000"))]
    disps = [
        Disposal(date(2025, 9, 1), D("100"), D("1200")),
        Disposal(date(2025, 5, 1), D("100"), D("1100")),
    ]
    records = match_disposals(acqs, disps)
    assert [m.disposal_date for m in records] == [date(2025, 5, 1), date(2025, 9, 1)]
