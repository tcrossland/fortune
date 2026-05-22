"""Opening-positions loading."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from banking_pipeline.opening_positions import load_opening_positions


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "opening-positions.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_round_trip_groups_lots_by_isin(tmp_path: Path) -> None:
    toml = """
[[lot]]
isin = "LU0128316170"
acquired = 2019-05-01
quantity = 5068.383
cost_gbp = 120000.00

[[lot]]
isin = "LU0128316170"
acquired = 2020-01-01
quantity = 100
cost_gbp = 2500.00

[[lot]]
isin = "IE00B3VWN518"
acquired = 2018-03-15
quantity = 50
cost_gbp = 1000.00
"""
    pos = load_opening_positions(_write(tmp_path, toml))
    assert set(pos) == {"LU0128316170", "IE00B3VWN518"}
    assert len(pos["LU0128316170"]) == 2
    assert pos["LU0128316170"][0].acquired == date(2019, 5, 1)
    assert pos["LU0128316170"][0].cost_gbp == Decimal("120000.00")


def test_rejects_malformed_code(tmp_path: Path) -> None:
    toml = (
        '[[lot]]\nisin = "FOO"\nacquired = 2019-05-01\n'
        "quantity = 1\ncost_gbp = 1.00\n"
    )
    with pytest.raises(ValidationError, match="not a valid ISIN"):
        load_opening_positions(_write(tmp_path, toml))


def test_accepts_structured_product_ref(tmp_path: Path) -> None:
    toml = (
        '[[lot]]\nisin = "ZZ00AB7IRH0"\nacquired = 2019-05-01\n'
        "quantity = 1\ncost_gbp = 1.00\n"
    )
    assert "ZZ00AB7IRH0" in load_opening_positions(_write(tmp_path, toml))
