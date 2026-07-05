"""Parser for Pictet's UK Tax Report (GBP capital gains + overseas income).

Synthetic text reproducing the report's layout — all figures and ISINs are
invented purely to exercise the parser.
"""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.pictet_uk_tax_extract import (
    PipelineFigures,
    has_material_finding,
    parse_uk_tax_report,
    reconcile_uk_tax,
)

D = Decimal

_REPORT = """\
3. Capital Gain

Total chargeable gain (disposals before 30 October 2024)
10,000.00
Total chargeable gain (disposals on or after 30 October 2024)
5,000.00
Total allowable loss
-3,000.00
Total deep discounted gains taxed to income
0.00
Total deep discounted securities losses
0.00
Total offshore gains taxed to income
1,200.00
Total exempt gain / loss
0.00

Security ISIN Quantity Cost (GBP) Proceeds (GBP) Gain/Loss (GBP)
ACME GLOBAL FUND A USD-ACC IE00B0000001 8,000.00 9,500.00 1,500.00
Sale 20/12/2024 Section 104 Holding 01/08/2023 442 3,000.00 3,600.00 600.00
Sale 04/04/2025 Section 104 Holding 01/08/2023 2,771 5,000.00 5,900.00 900.00
BETA BOND FUND B EUR-ACC IE00B0000002 4,000.00 3,700.00 -300.00
Sale 31/03/2025 Section 104 Holding 13/09/2024 738 4,000.00 3,700.00 -300.00
GAMMA TRANSFERRED IN LU0000000003 0.00 2,000.00 2,000.00
Sale 10/01/2025 Section 104 Holding 01/01/2020 100 0.00 2,000.00 2,000.00

2. Income
Gross Amount (GBP) Withholding Tax (GBP) Amount Received (GBP) Equalisation (GBP)
TOTAL Overseas Interest 47,000.00 0.00 47,000.00 100.00
TOTAL Overseas Dividend 19,000.00 100.00 18,900.00 50.00
Denmark Dividend
Company Dividends 400.00 100.00 300.00 0.00
"""


def test_parses_capital_gain_overview() -> None:
    ov = parse_uk_tax_report(_REPORT).overview
    assert ov is not None
    assert ov.gain_pre == D("10000.00")
    assert ov.gain_post == D("5000.00")
    assert ov.allowable_loss == D("-3000.00")
    assert ov.offshore_income_gain == D("1200.00")
    assert ov.deep_discounted_gain == D("0.00")
    assert ov.exempt == D("0.00")


def test_parses_per_security_lines_and_skips_sale_rows() -> None:
    secs = parse_uk_tax_report(_REPORT).securities
    by_isin = {s.isin: s for s in secs}
    assert set(by_isin) == {"IE00B0000001", "IE00B0000002", "LU0000000003"}
    assert by_isin["IE00B0000001"].gain_loss_gbp == D("1500.00")
    assert by_isin["IE00B0000001"].name == "ACME GLOBAL FUND A USD-ACC"
    assert by_isin["IE00B0000002"].gain_loss_gbp == D("-300.00")


def test_flags_zero_cost_transferred_in_security() -> None:
    by_isin = {s.isin: s for s in parse_uk_tax_report(_REPORT).securities}
    # A zero allowable cost = Pictet lacks the acquisition history (the pipeline
    # may cost it correctly), so it's an expected-divergence flag, not a bug.
    assert by_isin["LU0000000003"].estimated_cost is True
    assert by_isin["IE00B0000001"].estimated_cost is False


def test_parses_overseas_income_totals() -> None:
    inc = parse_uk_tax_report(_REPORT).income
    assert inc is not None
    assert inc.interest_gross == D("47000.00")
    assert inc.interest_wht == D("0.00")
    assert inc.dividend_gross == D("19000.00")
    assert inc.dividend_wht == D("100.00")


def test_empty_text_yields_empty_report() -> None:
    r = parse_uk_tax_report("nothing to see here\n")
    assert r.overview is None
    assert r.securities == ()
    assert r.income is None


# --- cross-check against SA108 / SA106 -------------------------------------


def _matching_pipeline(**overrides: Decimal | frozenset[str]) -> PipelineFigures:
    # Built to tie to the synthetic report's overview + security ISINs.
    base: dict[str, Decimal | frozenset[str]] = dict(
        chargeable_gains=D("15000.00"),
        allowable_loss=D("-3000.00"),
        offshore_income_gain=D("1200.00"),
        interest_gross=D("47000.00"),
        dividend_gross=D("19000.00"),
        disposal_isins=frozenset(
            {"IE00B0000001", "IE00B0000002", "LU0000000003"}
        ),
    )
    base.update(overrides)
    return PipelineFigures(**base)  # type: ignore[arg-type]


def test_reconcile_all_within_tolerance_and_present() -> None:
    findings = reconcile_uk_tax(parse_uk_tax_report(_REPORT), _matching_pipeline())
    assert all(f.status == "match" for f in findings)  # no mismatch, no presence
    assert not has_material_finding(findings)


def test_reconcile_flags_a_pictet_disposal_the_pipeline_is_missing() -> None:
    # The pipeline is missing IE00B0000002 that Pictet booked — the tax-critical
    # presence signal.
    pipeline = _matching_pipeline(
        disposal_isins=frozenset({"IE00B0000001", "LU0000000003"})
    )
    findings = reconcile_uk_tax(parse_uk_tax_report(_REPORT), pipeline)
    assert any(
        f.category == "presence" and f.label == "IE00B0000002"
        and f.status == "pictet_only"
        for f in findings
    )
    assert has_material_finding(findings)


def test_aggregate_mismatch_is_flagged_but_not_build_material() -> None:
    # £5,000 chargeable gain vs Pictet's £15,000 — flagged as a mismatch (shown
    # for review), but an aggregate divergence is FX-noise, not a build failure;
    # only a missing Pictet disposal (presence) gates the build.
    findings = reconcile_uk_tax(
        parse_uk_tax_report(_REPORT), _matching_pipeline(chargeable_gains=D("5000.00"))
    )
    cg = next(f for f in findings if f.label == "chargeable gains")
    assert cg.status == "mismatch"
    assert not has_material_finding(findings)


def test_reconcile_tolerance_absorbs_a_small_fx_difference() -> None:
    # £13,700 vs £15,000 = £1,300; 10% of £15,000 = £1,500 → within tolerance.
    findings = reconcile_uk_tax(
        parse_uk_tax_report(_REPORT), _matching_pipeline(chargeable_gains=D("13700.00"))
    )
    cg = next(f for f in findings if f.label == "chargeable gains")
    assert cg.status == "match"
