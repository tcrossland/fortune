"""UK tax-year boundary parsing."""

from __future__ import annotations

from datetime import date

import pytest

from banking_pipeline.tax.uk.tax_year import date_to_tax_year, tax_year_bounds


def test_bounds_basic() -> None:
    assert tax_year_bounds("2025-26") == (date(2025, 4, 6), date(2026, 4, 5))


def test_bounds_century_rollover() -> None:
    assert tax_year_bounds("2099-00") == (date(2099, 4, 6), date(2100, 4, 5))


@pytest.mark.parametrize("label", ["2025", "25-26", "2025/26", "2025-2026", ""])
def test_malformed_label_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        tax_year_bounds(label)


@pytest.mark.parametrize("label", ["2025-27", "2025-25", "2025-99"])
def test_ambiguous_label_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        tax_year_bounds(label)


def test_date_to_tax_year_before_april_6() -> None:
    assert date_to_tax_year(date(2026, 4, 5)) == "2025-26"
    assert date_to_tax_year(date(2026, 1, 10)) == "2025-26"


def test_date_to_tax_year_on_and_after_april_6() -> None:
    assert date_to_tax_year(date(2026, 4, 6)) == "2026-27"
    assert date_to_tax_year(date(2025, 12, 31)) == "2025-26"


def test_round_trip_bounds_within_year() -> None:
    start, end = tax_year_bounds("2025-26")
    assert date_to_tax_year(start) == "2025-26"
    assert date_to_tax_year(end) == "2025-26"
