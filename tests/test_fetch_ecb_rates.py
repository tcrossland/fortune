"""ECB fetcher triangulation (EUR-based reference rates → GBP-per-unit)."""

from __future__ import annotations

from decimal import Decimal

from scripts.fetch_ecb_rates import _triangulate

# ECB wide format: each cell is units of that currency per 1 EUR.
_WIDE = """Date, USD, JPY, GBP
2024-11-01, 1.0800, 165.00, 0.8300
2024-11-04, N/A, 164.00, 0.8320
2024-11-05, 1.0700, 163.00, N/A
"""


def _by_key(rows: list[tuple[str, str, Decimal]]) -> dict[tuple[str, str], Decimal]:
    return {(d, c): r for d, c, r in rows}


def test_triangulates_gbp_per_unit_from_eur_base() -> None:
    got = _by_key(_triangulate(_WIDE))
    q = Decimal("1e-8")
    # EUR row is the raw GBP cell (GBP per 1 EUR).
    assert got[("2024-11-01", "EUR")] == Decimal("0.8300")
    # GBP-per-USD = (GBP per EUR) / (USD per EUR).
    assert got[("2024-11-01", "USD")] == (Decimal("0.8300") / Decimal("1.0800")).quantize(q)
    assert got[("2024-11-01", "JPY")] == (Decimal("0.8300") / Decimal("165.00")).quantize(q)


def test_skips_na_cell_but_keeps_the_rest_of_the_row() -> None:
    got = _by_key(_triangulate(_WIDE))
    # 4 Nov: USD is N/A → no USD row, but EUR + JPY still produced.
    assert ("2024-11-04", "USD") not in got
    assert got[("2024-11-04", "EUR")] == Decimal("0.8320")
    assert ("2024-11-04", "JPY") in got


def test_skips_whole_row_when_gbp_fixing_missing() -> None:
    # 5 Nov: GBP is N/A → nothing can be triangulated, so the whole day drops.
    got = _by_key(_triangulate(_WIDE))
    assert not any(d == "2024-11-05" for d, _ in got)


def test_returns_empty_when_header_lacks_gbp() -> None:
    assert _triangulate("Date, USD, JPY\n2024-11-01, 1.08, 165.0\n") == []


def test_returns_empty_when_first_column_is_not_date() -> None:
    assert _triangulate("Foo, GBP\n2024-11-01, 0.83\n") == []
