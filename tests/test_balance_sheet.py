"""Phase-1 unit tests for the balance-sheet dataset transform.

All binary-free: feed fixture bean-query rows / directive text and assert
the assembled :class:`BalanceSheetData` and its compact JSON. The single
``bean-query`` shell-out lives behind ``build_data``; everything tested
here is the pure transform.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline import balance_sheet as bs
from banking_pipeline.bean_query import QueryResult
from banking_pipeline.commodities_metadata import CommodityMetadata

_ISIN = "US0378331005"


def _meta(isin: str = _ISIN, asset_class: str = "equity-etf") -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin,
        name="Apple Inc",
        domicile="US",
        reporting_status="reporting",
        asset_class=asset_class,  # type: ignore[arg-type]
        first_acquired=date(2020, 1, 1),
    )


class _FakeRates:
    def __init__(self, rates: dict[str, Decimal]) -> None:
        self._rates = rates

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return self._rates.get(currency)


# --- parsing ---------------------------------------------------------------


def test_parse_postings_flattens_and_drops_empty() -> None:
    result = QueryResult(
        rows=[
            ["2021-09-29", "Assets:Pic:K1:LU0955011761", "-1177.000 LU0955011761"],
            ["2021-09-29", "Assets:Pic:K1:Switch:EUR", "60344.79 EUR"],
            ["2021-09-30", "Assets:Pic:K1:USD", ""],  # zero leg → dropped
        ]
    )
    postings = bs.parse_postings(result)
    assert postings == (
        bs.Posting(date(2021, 9, 29), "Assets:Pic:K1:LU0955011761",
                   Decimal("-1177.000"), "LU0955011761"),
        bs.Posting(date(2021, 9, 29), "Assets:Pic:K1:Switch:EUR",
                   Decimal("60344.79"), "EUR"),
    )


def test_parse_price_directives_sorts_and_ignores_comments() -> None:
    text = (
        ";; header comment\n"
        "2021-09-06 price IE00BD904R66  113.2718 USD  ; source: 2021-K.beancount\n"
        "2021-09-03 price IE00BD904R66  110.00 USD  ; source: x\n"
        "not a directive\n"
    )
    series = bs.parse_price_directives(text)
    assert series == {
        "IE00BD904R66": (
            bs.PricePoint(date(2021, 9, 3), Decimal("110.00"), "USD"),
            bs.PricePoint(date(2021, 9, 6), Decimal("113.2718"), "USD"),
        )
    }


def test_parse_balance_assertions_with_and_without_tolerance() -> None:
    text = (
        ";; comment\n"
        "2021-08-01 balance Assets:Pic:K1:EUR  5000 ~ 0.5 EUR\n"
        "2021-09-01 balance Assets:Pic:K1:USD  -250.00 USD\n"
    )
    assert bs.parse_balance_assertions(text) == (
        bs.Assertion(date(2021, 8, 1), "Assets:Pic:K1:EUR", Decimal("5000"), "EUR"),
        bs.Assertion(date(2021, 9, 1), "Assets:Pic:K1:USD", Decimal("-250.00"), "USD"),
    )


# --- assembly --------------------------------------------------------------


def _sample_postings() -> tuple[bs.Posting, ...]:
    return (
        bs.Posting(date(2021, 1, 15), "Assets:Pic:K1:EUR", Decimal("1000"), "EUR"),
        bs.Posting(date(2021, 1, 20), f"Assets:Pic:K1:{_ISIN}", Decimal("10"), _ISIN),
    )


def test_assemble_bounds_fx_and_commodity_info() -> None:
    marks = {_ISIN: (bs.PricePoint(date(2021, 1, 10), Decimal("50"), "USD"),)}
    rates = _FakeRates({"EUR": Decimal("0.85"), "USD": Decimal("0.75")})

    data = bs.assemble(
        _sample_postings(), marks, {_ISIN: _meta()}, (), rate_source=rates
    )

    # Bounds span the earliest mark date through the latest posting date.
    assert data.as_of_min == date(2021, 1, 10)
    assert data.as_of_max == date(2021, 1, 20)
    # Security mark preserved; both currencies (cash EUR + quote USD) get a
    # synthesised GBP series — one monthly point at the month start.
    assert data.prices[_ISIN] == marks[_ISIN]
    assert data.prices["EUR"] == (bs.PricePoint(date(2021, 1, 1), Decimal("0.85"), "GBP"),)
    assert data.prices["USD"] == (bs.PricePoint(date(2021, 1, 1), Decimal("0.75"), "GBP"),)
    # GBP itself never gets a series (valued 1:1 client-side).
    assert "GBP" not in data.prices
    # Security display metadata; currencies carry none.
    assert data.commodities == {
        _ISIN: bs.CommodityInfo("Apple Inc", "equity-etf", "US")
    }


def test_assemble_missing_rate_currency_has_no_series() -> None:
    postings = (
        bs.Posting(date(2021, 2, 1), "Assets:Pic:K1:DKK", Decimal("500"), "DKK"),
    )
    # Rate source knows nothing about DKK → empty series, omitted entirely so
    # the browser flags the holding rather than valuing it at zero.
    data = bs.assemble(postings, {}, {}, (), rate_source=_FakeRates({}))
    assert "DKK" not in data.prices


def test_assemble_unknown_commodity_falls_back_to_other() -> None:
    isin = "LU0955011761"
    postings = (
        bs.Posting(date(2021, 1, 5), f"Assets:Pic:K1:{isin}", Decimal("3"), isin),
    )
    data = bs.assemble(postings, {}, {}, (), rate_source=_FakeRates({}))
    assert data.commodities == {isin: bs.CommodityInfo(isin, "other", "")}


# --- serialisation ---------------------------------------------------------


def test_to_json_uses_compact_keys_and_string_decimals() -> None:
    marks = {_ISIN: (bs.PricePoint(date(2021, 1, 10), Decimal("50"), "USD"),)}
    data = bs.assemble(
        _sample_postings(), marks, {_ISIN: _meta()}, (),
        rate_source=_FakeRates({"EUR": Decimal("0.85"), "USD": Decimal("0.75")}),
    )
    obj = json.loads(bs.to_json(data))

    assert obj["operating_currency"] == "GBP"
    assert obj["as_of_min"] == "2021-01-10"
    assert obj["as_of_max"] == "2021-01-20"
    assert {"d", "a", "q", "c"} == set(obj["postings"][0])
    # Decimals serialise as strings (no float precision loss).
    assert obj["postings"][0]["q"] == "1000"
    assert isinstance(obj["postings"][0]["q"], str)
    assert obj["prices"][_ISIN][0] == {"d": "2021-01-10", "p": "50", "c": "USD"}
    assert obj["commodities"][_ISIN]["asset_class"] == "equity-etf"


# --- orchestration boundary ------------------------------------------------


def test_build_data_degrades_when_bean_query_missing(
    monkeypatch, tmp_path: Path  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(
        bs, "run_query",
        lambda ledger, bql: QueryResult(binary_missing=True, error="nope"),
    )
    data, result = bs.build_data(
        tmp_path / "main.beancount",
        commodities={},
        rate_source=_FakeRates({}),
    )
    assert data is None
    assert result.binary_missing
