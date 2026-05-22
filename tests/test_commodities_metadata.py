"""Commodity metadata loading and beancount ``commodity`` emission."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from beancount import loader
from pydantic import ValidationError

from banking_pipeline import portfolio_aggregate
from banking_pipeline.commodities_metadata import (
    CommodityMetadata,
    load_commodities,
)
from banking_pipeline.portfolio_aggregate import _commodity_directives

_VALID_TOML = """
[[commodity]]
isin = "IE00B3VWN518"
name = "iShares Core MSCI World UCITS ETF"
domicile = "IE"
reporting_status = "reporting"
asset_class = "equity-etf"
first_acquired = 2018-03-15

[[commodity]]
isin = "LU1287023185"
name = "Amundi Euro Government Bond 7-10Y ETF"
domicile = "LU"
reporting_status = "non-reporting"
asset_class = "bond"
first_acquired = 2021-06-01
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "commodities.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_commodities_round_trip(tmp_path: Path) -> None:
    commodities = load_commodities(_write(tmp_path, _VALID_TOML))
    assert set(commodities) == {"IE00B3VWN518", "LU1287023185"}
    msci = commodities["IE00B3VWN518"]
    assert msci.name == "iShares Core MSCI World UCITS ETF"
    assert msci.domicile == "IE"
    assert msci.reporting_status == "reporting"
    assert msci.asset_class == "equity-etf"
    assert msci.first_acquired == date(2018, 3, 15)


def test_load_commodities_rejects_malformed_isin(tmp_path: Path) -> None:
    toml = """
[[commodity]]
isin = "XX00INVALID0"
name = "Bogus"
domicile = "IE"
reporting_status = "reporting"
asset_class = "equity-etf"
first_acquired = 2018-03-15
"""
    with pytest.raises(ValidationError, match="invalid ISIN"):
        load_commodities(_write(tmp_path, toml))


def test_load_commodities_rejects_unknown_reporting_status(tmp_path: Path) -> None:
    toml = """
[[commodity]]
isin = "IE00B3VWN518"
name = "iShares Core MSCI World UCITS ETF"
domicile = "IE"
reporting_status = "sort-of-reporting"
asset_class = "equity-etf"
first_acquired = 2018-03-15
"""
    with pytest.raises(ValidationError):
        load_commodities(_write(tmp_path, toml))


def test_load_commodities_rejects_duplicate_isin(tmp_path: Path) -> None:
    toml = _VALID_TOML + """
[[commodity]]
isin = "IE00B3VWN518"
name = "Duplicate"
domicile = "IE"
reporting_status = "reporting"
asset_class = "equity-etf"
first_acquired = 2019-01-01
"""
    with pytest.raises(ValueError, match="duplicate commodity entry"):
        load_commodities(_write(tmp_path, toml))


def test_commodity_directives_known_and_missing() -> None:
    commodities = {
        "IE00B3VWN518": CommodityMetadata(
            isin="IE00B3VWN518",
            name="iShares Core MSCI World UCITS ETF",
            domicile="IE",
            reporting_status="reporting",
            asset_class="equity-etf",
            first_acquired=date(2018, 3, 15),
        ),
    }
    # One known ISIN, one missing — the missing one stubs out.
    lines = _commodity_directives(["IE00B3VWN518", "LU1287023185"], commodities)
    assert lines == [
        # Stub (1970-01-01) sorts ahead of the dated known directive.
        "; missing metadata — add an entry to data/commodities.toml",
        "1970-01-01 commodity LU1287023185",
        '  reporting-status: "unknown"',
        "",
        "2018-03-15 commodity IE00B3VWN518",
        '  name: "iShares Core MSCI World UCITS ETF"',
        '  isin: "IE00B3VWN518"',
        '  domicile: "IE"',
        '  reporting-status: "reporting"',
        '  asset-class: "equity-etf"',
        "",
    ]


def _write_year_file(data_dir: Path) -> None:
    (data_dir / "2024.beancount").write_text(
        "2024-03-01 * \"Buy\"\n"
        "  Assets:Pic:K123456001:IE00B3VWN518   100 IE00B3VWN518 {50.00 EUR}\n"
        "  Assets:Pic:K123456001:EUR          -5000.00 EUR\n",
        encoding="utf-8",
    )


def test_discover_isins_finds_traded_securities(tmp_path: Path) -> None:
    _write_year_file(tmp_path)
    assert portfolio_aggregate.discover_isins(tmp_path) == {"IE00B3VWN518"}


def test_generate_emits_commodity_block_above_opens(tmp_path: Path) -> None:
    _write_year_file(tmp_path)
    commodities = {
        "IE00B3VWN518": CommodityMetadata(
            isin="IE00B3VWN518",
            name="iShares Core MSCI World UCITS ETF",
            domicile="IE",
            reporting_status="reporting",
            asset_class="equity-etf",
            first_acquired=date(2018, 3, 15),
        ),
    }
    output, _ = portfolio_aggregate.generate(
        tmp_path, commodities=commodities, operating_currencies=("GBP",)
    )
    text = output.read_text(encoding="utf-8")
    assert "2018-03-15 commodity IE00B3VWN518" in text
    assert '  reporting-status: "reporting"' in text
    # Commodity directive precedes the first account open. ("open Assets"
    # only matches a real open line, not the word "open" in the header.)
    assert text.index("commodity IE00B3VWN518") < text.index("open Assets")


def test_generate_without_commodities_emits_no_block(tmp_path: Path) -> None:
    _write_year_file(tmp_path)
    output, _ = portfolio_aggregate.generate(tmp_path)
    text = output.read_text(encoding="utf-8")
    # The word "commodity" appears in the header prose; assert no
    # directive / section header was emitted.
    assert "commodity IE00B3VWN518" not in text
    assert ";; UK-tax commodity metadata." not in text


def test_generate_stubs_missing_metadata(tmp_path: Path) -> None:
    _write_year_file(tmp_path)
    # Empty metadata dict => the in-use ISIN gets a stub directive.
    output, _ = portfolio_aggregate.generate(tmp_path, commodities={})
    text = output.read_text(encoding="utf-8")
    assert "; missing metadata — add an entry to data/commodities.toml" in text
    assert "1970-01-01 commodity IE00B3VWN518" in text
    assert '  reporting-status: "unknown"' in text


def test_emitted_commodity_directive_parses_in_beancount() -> None:
    commodities = {
        "IE00B3VWN518": CommodityMetadata(
            isin="IE00B3VWN518",
            name="iShares Core MSCI World UCITS ETF",
            domicile="IE",
            reporting_status="reporting",
            asset_class="equity-etf",
            first_acquired=date(2018, 3, 15),
        ),
    }
    ledger = "\n".join(_commodity_directives(["IE00B3VWN518"], commodities))
    _, errors, _ = loader.load_string(ledger)
    assert not errors, [e.message for e in errors]
