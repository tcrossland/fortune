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
    normalise_commodity_code,
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
    with pytest.raises(ValidationError, match="not a valid ISIN"):
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


def test_deeply_discounted_defaults_false_and_parses(tmp_path: Path) -> None:
    assert load_commodities(_write(tmp_path, _VALID_TOML))[
        "IE00B3VWN518"
    ].deeply_discounted is False
    toml = """
[[commodity]]
isin = "DE000BU3Z005"
name = "Discounted bond"
domicile = "DE"
reporting_status = "uk-domestic"
asset_class = "bond"
first_acquired = 2023-11-24
deeply_discounted = true
"""
    assert load_commodities(_write(tmp_path, toml))[
        "DE000BU3Z005"
    ].deeply_discounted is True


def test_distributions_as_interest_defaults_false_and_parses(
    tmp_path: Path,
) -> None:
    assert load_commodities(_write(tmp_path, _VALID_TOML))[
        "IE00B3VWN518"
    ].distributions_as_interest is False
    toml = """
[[commodity]]
isin = "LU2096759431"
name = "JPM Income Fund"
domicile = "LU"
reporting_status = "reporting"
asset_class = "bond"
first_acquired = 2024-09-12
distributions_as_interest = true
"""
    assert load_commodities(_write(tmp_path, toml))[
        "LU2096759431"
    ].distributions_as_interest is True


def test_accepts_structured_product_internal_ref(tmp_path: Path) -> None:
    # Pictet structured products carry an 11-char internal ref, not an
    # ISIN — they still need metadata for tax classification.
    toml = """
[[commodity]]
isin = "ZZ00AB7IRH0"
name = "Pictet structured note"
domicile = "CH"
reporting_status = "uk-domestic"
asset_class = "other"
first_acquired = 2024-04-19
"""
    commodities = load_commodities(_write(tmp_path, toml))
    assert "ZZ00AB7IRH0" in commodities
    assert commodities["ZZ00AB7IRH0"].asset_class == "other"


def test_normalise_commodity_code() -> None:
    # Valid ISIN → normalised; 11-char internal ref → accepted; a 12-char
    # checksum-failing code (likely typo) and short garbage → rejected.
    assert normalise_commodity_code("ie00b3vwn518") == "IE00B3VWN518"
    assert normalise_commodity_code("ZZ00AB7IRH0") == "ZZ00AB7IRH0"
    assert normalise_commodity_code("ZZ00ABB5K5 0") == "ZZ00ABB5K50"  # space artifact
    assert normalise_commodity_code("XX00INVALID0") is None  # 12-char, bad checksum
    assert normalise_commodity_code("FOO") is None
    # Allow-listed Vanguard tickers (keyed by ticker, no ISIN) are accepted;
    # an unlisted short code is still rejected.
    assert normalise_commodity_code("VGVA") == "VGVA"
    assert normalise_commodity_code("vmig") == "VMIG"
    assert normalise_commodity_code("ZZZZ") is None


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


def test_three_letter_name_segment_not_constrained_as_currency(
    tmp_path: Path,
) -> None:
    # A 3-letter account-name segment that is *not* a currency (a
    # counterparty label) must open without a commodity constraint —
    # otherwise EUR postings to it are rejected as an invalid currency.
    (tmp_path / "2023.beancount").write_text(
        '2023-01-19 * "Pago entrante" "IBM earnout"\n'
        "  Assets:Pic:K123456001:EUR     1150000.00 EUR\n"
        "  Income:External:Earnout:IBM  -1150000.00 EUR\n",
        encoding="utf-8",
    )
    output, _ = portfolio_aggregate.generate(tmp_path, operating_currencies=("GBP",))
    text = output.read_text(encoding="utf-8")
    # The cash account keeps its real currency constraint…
    assert "open Assets:Pic:K123456001:EUR EUR" in text
    # …but the IBM income account opens unconstrained (no "… IBM").
    assert "open Income:External:Earnout:IBM\n" in text
    assert "open Income:External:Earnout:IBM IBM" not in text


def test_generate_excludes_nested_aggregate_files(tmp_path: Path) -> None:
    # A stale / per-account aggregate (a *.beancount that itself includes
    # other files) must not be swept in as a per-year source — otherwise
    # the master aggregate re-includes everything it pulls in and
    # bean-check reports "Duplicate filename parsed".
    _write_year_file(tmp_path)
    (tmp_path / "k123456001.beancount").write_text(
        ';; Portfolio aggregate.\noption "operating_currency" "GBP"\n'
        'include "2024.beancount"\n',
        encoding="utf-8",
    )
    output, _ = portfolio_aggregate.generate(tmp_path)
    text = output.read_text(encoding="utf-8")
    assert 'include "2024.beancount"' in text
    assert 'include "k123456001.beancount"' not in text


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


# --- issuer inference -----------------------------------------------------


def test_infer_issuer_from_real_fund_names() -> None:
    from banking_pipeline.commodities_metadata import infer_issuer

    cases = {
        "iShares IV PLC - iShares Lithium & Battery Producers UCITS ETF": "iShares",
        "HSBC ETFs PLC - HSBC S&P India Tech UCITS ETF EUR-Acc.-": "HSBC",
        "Wisdomtree Issuer ICAV - Wisdomtree Europe Defence": "WisdomTree",
        "Multi Units Luxembourg SICAV - Amundi Euro Government Bond": "Amundi",
        "PICTET-CLEAN ENERGY TR.-I EUR": "Pictet",
        "PWM FS-GLOBAL REITS SEL.HI EUR": "Pictet",
        "SISF-QEP GLOBAL ESG C USD -ACC.-": "Schroder",
        "SSGA-GBL TRE.BD IDX S EUR PO.H.-ACC": "State Street",
        "JPMF-GBL NAT.RESOURCE.JPM I EUR-ACC": "JPMorgan",
        "AB SICAV I-SUST.US THEM.I USD-ACC": "AllianceBernstein",
    }
    for name, issuer in cases.items():
        assert infer_issuer(name) == issuer, name
    # A direct equity / sovereign bond has no fund house.
    assert infer_issuer("NOVO NORDISK 'B'") is None
    assert infer_issuer("2.30% GERMANY 23/33 SR GREEN") is None


def test_resolved_issuer_explicit_overrides_inference() -> None:
    base = dict(
        domicile="IE", asset_class="equity-etf",
        reporting_status="reporting", first_acquired=date(2020, 1, 1),
    )
    # No explicit issuer → inferred from the name.
    inferred = CommodityMetadata(
        isin="IE00B3VWN518", name="iShares Core MSCI World", **base
    )
    assert inferred.resolved_issuer == "iShares"
    # Explicit issuer wins over what the name would infer.
    overridden = CommodityMetadata(
        isin="IE00B3VWN518", name="iShares Core MSCI World",
        issuer="BlackRock", **base
    )
    assert overridden.resolved_issuer == "BlackRock"
    # Neither resolves → None (an unknown bucket downstream).
    unknown = CommodityMetadata(
        isin="IE00B3VWN518", name="Some Unbranded Bond 24/30", **base
    )
    assert unknown.resolved_issuer is None


def test_infer_issuer_precedence_case_and_no_false_match() -> None:
    from banking_pipeline.commodities_metadata import infer_issuer

    # Specific fragment wins over a generic one it could collide with.
    assert infer_issuer("AB SICAV I-SUST.US THEM.I USD-ACC") == "AllianceBernstein"
    assert infer_issuer("ABERDEEN II-EURO.CORP.BD D EUR") == "abrdn"
    assert infer_issuer("Multi Units Luxembourg SICAV - Amundi Euro Govt") == "Amundi"
    # Matching is case-insensitive.
    assert infer_issuer("pictet-biotech-i usd") == "Pictet"
    # A name with no recognised house → None, not a spurious match.
    assert infer_issuer("GLOBAL GOVERNMENT BOND 2030 FUND") is None
    assert infer_issuer("2.30% GERMANY 23/33 SR GREEN") is None
    # Known limitation (documented, not a bug): matching is substring-based,
    # so an embedded fragment *does* match. This pins the behaviour so a
    # future refactor to word-boundary matching notices the change.
    assert infer_issuer("SOMETHING WITH JPM EMBEDDED") == "JPMorgan"


# --- statement-name index (P mandate by-name resolution) ------------------


def test_normalise_security_name_folds_case_and_punctuation() -> None:
    from banking_pipeline.commodities_metadata import normalise_security_name

    # The statement's abbreviated display form and the stored name normalise
    # equal once case, punctuation, and spacing are folded.
    assert normalise_security_name("Novo Nordisk 'B'") == normalise_security_name(
        "NOVO NORDISK 'B'"
    )
    assert normalise_security_name(
        "Btc (Coinshares) -Etc- 21/Perp"
    ) == normalise_security_name("BTC (COINSHARES) -ETC- 21/PERP")
    # Genuinely different names stay different (they need an explicit alias).
    assert normalise_security_name(
        "Hanetf-Sprott Glb Uran.Mini.Etf Usd"
    ) != normalise_security_name("HANetf ICAV - Sprott Global Uranium Mining")


def test_build_statement_name_index_auto_matches_name_and_aliases() -> None:
    from banking_pipeline.commodities_metadata import build_statement_name_index

    base = dict(
        domicile="IE", asset_class="equity-etf",
        reporting_status="reporting", first_acquired=date(2025, 2, 1),
    )
    commodities = {
        # Auto-matches on ``name`` (statement prints the same short form).
        "DK0062498333": CommodityMetadata(
            isin="DK0062498333", name="NOVO NORDISK 'B'", **base
        ),
        # Long contract-note name; the statement short form needs an alias.
        "IE0005YK6564": CommodityMetadata(
            isin="IE0005YK6564",
            name="HANetf ICAV - Sprott Global Uranium Mining UCITS ETF",
            statement_names=("Hanetf-Sprott Glb Uran.Mini.Etf Usd",),
            **base,
        ),
    }
    index = build_statement_name_index(commodities)
    from banking_pipeline.commodities_metadata import normalise_security_name

    assert index[normalise_security_name("Novo Nordisk 'B'")] == "DK0062498333"
    assert (
        index[normalise_security_name("Hanetf-Sprott Glb Uran.Mini.Etf Usd")]
        == "IE0005YK6564"
    )


def test_build_statement_name_index_raises_on_ambiguous_name() -> None:
    from banking_pipeline.commodities_metadata import build_statement_name_index

    base = dict(
        domicile="IE", asset_class="equity-etf",
        reporting_status="reporting", first_acquired=date(2025, 2, 1),
    )
    # Two commodities normalising to the same name would assert a quantity
    # against the wrong ISIN — must fail loudly.
    commodities = {
        "IE0005YK6564": CommodityMetadata(
            isin="IE0005YK6564", name="Acme Fund", **base
        ),
        "IE0008119MO8": CommodityMetadata(
            isin="IE0008119MO8", name="ACME  FUND",
            statement_names=("acme fund",), **base
        ),
    }
    with pytest.raises(ValueError, match="ambiguous"):
        build_statement_name_index(commodities)
