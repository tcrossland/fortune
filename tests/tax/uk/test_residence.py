"""UK residence + FIG-window helpers."""

from __future__ import annotations

from datetime import date

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.tax.uk.residence import (
    fig_eligible_years,
    gain_is_foreign,
    ineligible_claims,
    is_pre_residence,
    is_pre_residence_year,
    residence_start_year,
)


def _meta(**kw: object) -> CommodityMetadata:
    base: dict[str, object] = dict(
        isin="IE00B3VWN518",
        name="x",
        domicile="IE",
        reporting_status="reporting",
        asset_class="equity-etf",
        first_acquired=date(2020, 1, 1),
    )
    base.update(kw)
    return CommodityMetadata.model_validate(base)


def test_residence_start_year_on_tax_year_boundary() -> None:
    assert residence_start_year(date(2025, 6, 1)) == "2025-26"
    assert residence_start_year(date(2025, 4, 6)) == "2025-26"
    assert residence_start_year(date(2025, 4, 5)) == "2024-25"


def test_pre_residence_year_and_date() -> None:
    arrival = date(2025, 6, 1)
    assert is_pre_residence_year("2024-25", arrival) is True
    assert is_pre_residence_year("2025-26", arrival) is False
    assert is_pre_residence_year("2024-25", None) is False
    assert is_pre_residence(date(2025, 5, 31), arrival) is True
    assert is_pre_residence(date(2025, 6, 1), arrival) is False
    assert is_pre_residence(date(2020, 1, 1), None) is False


def test_fig_window_four_years_from_arrival() -> None:
    assert fig_eligible_years(date(2025, 6, 1)) == frozenset(
        {"2025-26", "2026-27", "2027-28", "2028-29"}
    )
    # Resident since 2023-24: only the 2025-26 / 2026-27 remainder of the
    # 4-year window is eligible (regime starts 2025-26).
    assert fig_eligible_years(date(2023, 6, 1)) == frozenset(
        {"2025-26", "2026-27"}
    )
    assert fig_eligible_years(None) == frozenset()


def test_ineligible_claims_flags_outside_window() -> None:
    arrival = date(2025, 6, 1)
    assert ineligible_claims(frozenset({"2025-26"}), arrival) == []
    assert ineligible_claims(
        frozenset({"2025-26", "2030-31"}), arrival
    ) == ["2030-31"]


def test_gain_is_foreign_derivation_and_override() -> None:
    assert gain_is_foreign(None) is False  # unknown situs → no relief
    assert gain_is_foreign(_meta(domicile="IE")) is True
    assert gain_is_foreign(_meta(domicile="GB")) is False
    assert gain_is_foreign(_meta(reporting_status="uk-domestic")) is False
    # Explicit flag overrides a foreign domicile.
    assert gain_is_foreign(_meta(domicile="IE", uk_situs=True)) is False
    assert gain_is_foreign(_meta(domicile="GB", uk_situs=False)) is True
