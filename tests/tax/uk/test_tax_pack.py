"""tax-pack Markdown renderer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline.tax.uk.cgt_allowance import apply_cgt_allowances
from banking_pipeline.tax.uk.currency import RateGap
from banking_pipeline.tax.uk.eri import EriResult
from banking_pipeline.tax.uk.residence import FigDesignationRow
from banking_pipeline.tax.uk.sa106 import Sa106DividendRow, Sa106Report
from banking_pipeline.tax.uk.sa108 import Sa108Report, Sa108Row
from banking_pipeline.tax.uk.tax_pack import render_tax_pack

D = Decimal


def _cgt_row(**kw: object) -> Sa108Row:
    base: dict[str, object] = dict(
        disposal_date=date(2025, 6, 1),
        isin="IE00B3VWN518",
        commodity_name="World ETF",
        reporting_status="reporting",
        quantity=D(100),
        proceeds_gbp=D(5000),
        cost_gbp=D(1000),
        gain_gbp=D(4000),
        match_type="s104",
        acquisition_dates=[date(2024, 5, 1)],
    )
    base.update(kw)
    return Sa108Row(**base)  # type: ignore[arg-type]


def _allowance():  # type: ignore[no-untyped-def]
    return apply_cgt_allowances(
        tax_year="2025-26",
        gains_pre=D(4000),
        gains_post=D(0),
        current_year_losses=D(0),
        brought_forward=D(0),
        annual_exempt_amount=D(3000),
        rate_split=False,
    )


def test_renders_core_sections_and_caveat() -> None:
    sa108 = Sa108Report(
        rows=[
            _cgt_row(),
            _cgt_row(isin="LU1287023185", reporting_status="non-reporting",
                     gain_gbp=D(150), proceeds_gbp=D(250), cost_gbp=D(100)),
        ],
        dds_disposals=[
            _cgt_row(isin="XS0000000000", gain_gbp=D(200), proceeds_gbp=D(200),
                     cost_gbp=D(0)),
        ],
    )
    sa106 = Sa106Report(
        dividends=[
            Sa106DividendRow(
                country="US", isin="US0378331005", commodity_name="Apple",
                gross_gbp=D(80), wht_gbp=D(12), net_gbp=D(68), document_count=1,
            )
        ],
    )
    md = render_tax_pack(
        year="2025-26", sa108=sa108, sa106=sa106, eri=EriResult(rows=[]),
        allowance=_allowance(), designation=[], fig_claimed=False,
    )
    assert "# UK tax pack — 2025-26" in md
    assert "not tax advice" in md.lower()
    assert "verify each against the SA108 / SA106 for 2025-26" in md
    # CGT boxes + computation.
    assert "## Capital gains — SA108" in md
    assert "| 23 | Number of disposals | 1 |" in md
    assert "| 24 | Disposal proceeds | £5,000.00 |" in md
    assert "Annual exempt amount: £3,000.00" in md
    assert "Taxable gain: £1,000.00" in md
    # Foreign dividends + FTCR.
    assert "## Foreign dividends — SA106" in md
    assert "Foreign tax (for Foreign Tax Credit Relief): £12.00" in md
    # Offshore income gain + deeply discounted.
    assert "## Offshore income gains — SA106" in md
    assert "£150.00" in md
    assert "## Deeply discounted securities" in md
    # No FIG section when not claimed.
    assert "Foreign Income & Gains" not in md


def test_fig_section_and_coverage_warning() -> None:
    sa108 = Sa108Report(rows=[_cgt_row(reporting_status="uk-domestic")])
    md = render_tax_pack(
        year="2025-26", sa108=sa108, sa106=Sa106Report(dividends=[]),
        eri=EriResult(rows=[]), allowance=_allowance(),
        designation=[
            FigDesignationRow(
                "gain", "capital gain", "IE", "IE00B3VWN518", "World ETF", D(4000)
            ),
            FigDesignationRow(
                "loss", "capital gain", "LU", "LU0000000001", "Euro ETF", D(-1500)
            ),
        ],
        fig_claimed=True,
        rate_gaps=[RateGap(isin="LU1287023185", currency="EUR", month="2025-06")],
    )
    assert "## Foreign Income & Gains (FIG) claim — SA109" in md
    assert "personal allowance and the CGT annual exempt amount are forfeited" in md
    assert "| gain | capital gain | IE | IE00B3VWN518 | £4,000.00 |" in md
    assert "| loss | capital gain | LU | LU0000000001 | £-1,500.00 |" in md
    # The disallowed loss is surfaced as its own subtotal, not netted away.
    assert "- Non-UK gains relieved: £4,000.00" in md
    assert "Disallowed foreign losses (loss relief forfeited): £-1,500.00" in md
    assert "loss relief forfeited" in md
    # Coverage warning naming the missing rate.
    assert "missing GBP rates" in md
    assert "EUR 2025-06 (LU1287023185)" in md


def test_unclassified_holding_flagged_as_missing_relief_under_claim() -> None:
    """An unknown-status disposal defaults to UK situs and is neither
    taxed nor relieved; under a FIG claim the pack must flag it as
    possibly missing relief."""

    sa108 = Sa108Report(
        rows=[
            _cgt_row(reporting_status="uk-domestic"),
            _cgt_row(isin="XS9999999999", commodity_name="Mystery Bond",
                     reporting_status="unknown"),
        ]
    )
    md = render_tax_pack(
        year="2025-26", sa108=sa108, sa106=Sa106Report(dividends=[]),
        eri=EriResult(rows=[]), allowance=_allowance(),
        designation=[], fig_claimed=True,
    )
    assert "## ⚠️ Unclassified holdings — may be missing FIG relief" in md
    assert "- XS9999999999 — Mystery Bond" in md
    assert "missing relief" in md.lower()


def test_unclassified_holding_not_flagged_without_claim() -> None:
    """The missing-relief callout is FIG-specific — no claim, no section."""

    sa108 = Sa108Report(
        rows=[_cgt_row(isin="XS9999999999", reporting_status="unknown")]
    )
    md = render_tax_pack(
        year="2025-26", sa108=sa108, sa106=Sa106Report(dividends=[]),
        eri=EriResult(rows=[]), allowance=_allowance(),
        designation=[], fig_claimed=False,
    )
    assert "Unclassified holdings" not in md
