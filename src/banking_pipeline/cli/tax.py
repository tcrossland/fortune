"""Tax reporting and planning commands.

The UK-tax CLI surface: ``tax-report`` (SA108/SA106 + CGT loss chain),
``tax-forecast``, ``tax-pack``, and ``fig-advice``, plus their CSV/summary
writers and the shared :class:`_TaxComputation` loader. All read the JSONL
sidecars via ``_load_sidecar_transactions`` (kept in :mod:`...cli._main`).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated

import typer

from banking_pipeline.cgt_losses import load_cgt_brought_forward_losses
from banking_pipeline.cli._main import (
    _configure_logging,
    _load_sidecar_transactions,
    app,
    err_console,
)
from banking_pipeline.cli_options import (
    VerboseOpt,
)
from banking_pipeline.commodities_metadata import load_commodities
from banking_pipeline.config import settings
from banking_pipeline.fx.gbp_rates import build_rate_source
from banking_pipeline.opening_positions import load_opening_positions
from banking_pipeline.tax.uk.cgt_allowance import (
    CGT_STATUSES,
    CgtAllowanceResult,
    loss_carryforward_chain,
)
from banking_pipeline.tax.uk.currency import RateGap
from banking_pipeline.tax.uk.eri import EriResult, compute_eri, load_eri
from banking_pipeline.tax.uk.fig_advice import (
    FigPattern,
    FigYearInputs,
    evaluate_fig_window,
)
from banking_pipeline.tax.uk.liability import LiabilityResult, compute_liability
from banking_pipeline.tax.uk.rates import CgtRateSchedule, IncomeTaxBands
from banking_pipeline.tax.uk.residence import (
    FigDesignationRow,
    FigKind,
    fig_eligible_years,
    fig_subtotals,
    ineligible_claims,
    is_pre_residence_year,
)
from banking_pipeline.tax.uk.sa106 import Sa106Report, compute_sa106_dividends
from banking_pipeline.tax.uk.sa108 import (
    MatchedHistory,
    Sa108Report,
    Sa108Row,
    compute_sa108,
    match_history,
)
from banking_pipeline.tax.uk.tax_pack import render_tax_pack
from banking_pipeline.tax.uk.tax_year import date_to_tax_year, tax_year_bounds


@dataclass
class _TaxComputation:
    """The year's base tax figures, shared by tax-report / -forecast / -pack.

    Holds the raw (un-FIG-partitioned) reports plus the inputs each command
    needs for its own downstream: the loss chain (run once here with the
    configured claims for report/pack; re-run per scenario by the
    forecast), the FIG context, and the GBP rate-coverage gaps.
    """

    eri_result: EriResult
    sa108: Sa108Report
    sa106: Sa106Report
    history: MatchedHistory
    pre_ledger_losses: Decimal
    arrival: date | None
    fig_claim_years: frozenset[str]
    fig_claimed: bool
    rate_gaps: list[RateGap]


def _compute_tax_year(
    *,
    year: str,
    source: Path,
    commodities: Path | None,
    rate_source: str | None,
    opening_positions: Path | None,
    eri: Path | None,
) -> _TaxComputation:
    """Load the sidecars and compute the base SA108 / SA106 / ERI figures.

    Centralises the load-and-compute the three tax commands share (ISA
    exclusion, GBP rate sourcing, section 104 matching, residence-aware
    income/disposal filtering). The loss chain and any FIG partition are
    left to the caller, since the forecast runs the chain per claim
    scenario while report/pack run it once.
    """

    cpath = commodities or settings.commodities_metadata_path
    commodities_map = (
        load_commodities(cpath) if cpath is not None and cpath.is_file() else {}
    )
    eff_settings = (
        settings.model_copy(update={"gbp_rate_source": rate_source})
        if rate_source is not None
        else settings
    )
    rates = build_rate_source(eff_settings)
    opening_path = opening_positions or settings.opening_positions_path
    opening = (
        load_opening_positions(opening_path)
        if opening_path is not None and opening_path.is_file()
        else {}
    )
    eri_path = eri or settings.eri_path
    eri_entries = (
        load_eri(eri_path) if eri_path is not None and eri_path.is_file() else {}
    )
    arrival = settings.uk_residence_start_date

    # Single tax-exemption choke point: ISA-wrapped transactions are
    # tax-free and never reach any computation.
    txns = [tx for tx in _load_sidecar_transactions(source) if not tx.is_tax_exempt]
    eri_result = compute_eri(
        txns, tax_year_label=year, eri_entries=eri_entries,
        commodities=commodities_map, opening_positions=opening, source=rates,
    )
    sa108 = compute_sa108(
        txns, tax_year_label=year, commodities=commodities_map, source=rates,
        rate_change_date=settings.cgt_rate_change_dates.get(year),
        opening_positions=opening, cost_adjustments=eri_result.base_cost_adjustments,
        arrival=arrival,
    )
    sa106 = compute_sa106_dividends(
        txns, tax_year_label=year, commodities=commodities_map, source=rates,
        arrival=arrival,
    )
    history = match_history(
        txns, commodities=commodities_map, source=rates,
        opening_positions=opening, cost_adjustments=eri_result.base_cost_adjustments,
    )
    losses_path = settings.cgt_losses_path
    pre_ledger_losses = (
        load_cgt_brought_forward_losses(losses_path)
        if losses_path is not None and losses_path.is_file()
        else Decimal(0)
    )
    return _TaxComputation(
        eri_result=eri_result,
        sa108=sa108,
        sa106=sa106,
        history=history,
        pre_ledger_losses=pre_ledger_losses,
        arrival=arrival,
        fig_claim_years=settings.fig_claim_years,
        fig_claimed=year in settings.fig_claim_years,
        rate_gaps=sa108.missing_rates + sa106.missing_rates + eri_result.missing_rates,
    )


def _money(value: Decimal) -> str:
    """Format a GBP amount as a plain 2-dp string (no scientific notation).

    ``Decimal`` arithmetic can yield exponent forms like ``0E-10`` (e.g.
    ``Decimal(0) * rate``); quantizing to pennies renders ``0.00`` and
    keeps every figure fixed-point for the CSVs.
    """

    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _qty(value: Decimal) -> str:
    """Format a unit quantity fixed-point (no scientific notation), keeping
    its own precision rather than forcing pennies."""

    return format(value, "f")


def _write_sa108_csv(path: Path, report: Sa108Report) -> int:
    """Write the CGT disposals (reporting / uk-domestic). Returns row count."""

    rows = [r for r in report.rows if r.reporting_status in CGT_STATUSES]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "disposal_date", "isin", "commodity_name", "reporting_status",
            "quantity", "proceeds_gbp", "cost_gbp", "gain_gbp", "match_type",
            "period", "acquisition_dates",
        ])
        for r in rows:
            writer.writerow([
                r.disposal_date.isoformat(), r.isin, r.commodity_name,
                r.reporting_status, _qty(r.quantity), _money(r.proceeds_gbp),
                _money(r.cost_gbp), _money(r.gain_gbp), r.match_type, r.period,
                ";".join(d.isoformat() for d in r.acquisition_dates),
            ])
    return len(rows)


def _write_sa106_dividends_csv(path: Path, report: Sa106Report) -> int:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "country", "isin", "commodity_name", "gross_gbp", "wht_gbp",
            "net_gbp", "document_count",
        ])
        for r in report.dividends:
            writer.writerow([
                r.country, r.isin, r.commodity_name, _money(r.gross_gbp),
                _money(r.wht_gbp), _money(r.net_gbp), r.document_count,
            ])
    return len(report.dividends)


def _write_sa106_interest_csv(path: Path, report: Sa106Report) -> int:
    """Foreign interest — distributions from >60%-interest-bearing
    offshore funds (the UK 'bond fund' rule). Returns the row count."""

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "country", "isin", "commodity_name", "gross_gbp", "wht_gbp",
            "net_gbp", "document_count",
        ])
        for r in report.interest:
            writer.writerow([
                r.country, r.isin, r.commodity_name, _money(r.gross_gbp),
                _money(r.wht_gbp), _money(r.net_gbp), r.document_count,
            ])
    return len(report.interest)


def _write_offshore_income_gains_csv(path: Path, report: Sa108Report) -> int:
    """Write disposals of non-reporting funds — taxed as offshore income
    gains (SA106), not CGT. Same per-disposal shape as the SA108 file
    minus the (uniformly ``non-reporting``) status column. Returns rows."""

    rows = [r for r in report.rows if r.reporting_status == "non-reporting"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "disposal_date", "isin", "commodity_name", "quantity",
            "proceeds_gbp", "cost_gbp", "gain_gbp", "match_type",
            "acquisition_dates",
        ])
        for r in rows:
            writer.writerow([
                r.disposal_date.isoformat(), r.isin, r.commodity_name,
                _qty(r.quantity), _money(r.proceeds_gbp), _money(r.cost_gbp),
                _money(r.gain_gbp), r.match_type,
                ";".join(d.isoformat() for d in r.acquisition_dates),
            ])
    return len(rows)


def _write_deep_discounted_csv(path: Path, report: Sa108Report) -> int:
    """Write deeply discounted security disposals — gain taxed as income,
    loss generally not allowable. Returns the row count."""

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "disposal_date", "isin", "commodity_name", "quantity",
            "proceeds_gbp", "cost_gbp", "gain_gbp", "match_type",
            "acquisition_dates",
        ])
        for r in report.dds_disposals:
            writer.writerow([
                r.disposal_date.isoformat(), r.isin, r.commodity_name,
                _qty(r.quantity), _money(r.proceeds_gbp), _money(r.cost_gbp),
                _money(r.gain_gbp), r.match_type,
                ";".join(d.isoformat() for d in r.acquisition_dates),
            ])
    return len(report.dds_disposals)


def _write_eri_csv(path: Path, eri: EriResult) -> int:
    """Write excess reportable income split by income type. ``gross_gbp``
    is the taxable income; ``base_cost_adjustment_gbp`` (gross less
    equalisation) is the section 104 pool uplift. Returns the row count."""

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "country", "isin", "commodity_name", "income_type",
            "taxable_income_gbp", "equalisation_gbp",
            "base_cost_adjustment_gbp", "event_count",
        ])
        for r in eri.rows:
            writer.writerow([
                r.country, r.isin, r.commodity_name, r.income_type,
                _money(r.gross_gbp), _money(r.equalisation_gbp),
                _money(r.base_cost_adjustment_gbp), r.event_count,
            ])
    return len(eri.rows)


def _write_cgt_carryforward_csv(
    path: Path, chain: dict[str, CgtAllowanceResult]
) -> int:
    """Write the year-by-year CGT allowance / loss-carry-forward chain so
    the brought-forward figures are auditable. Returns the row count."""

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "tax_year", "gains_pre", "gains_post", "current_year_losses",
            "net_gain", "current_year_loss_carried", "bf_losses_available",
            "bf_losses_used", "annual_exempt_amount", "annual_exempt_used",
            "taxable_pre", "taxable_post", "taxable_total",
            "losses_carried_forward",
        ])
        for label in sorted(chain):
            r = chain[label]
            writer.writerow([
                r.tax_year, _money(r.gains_pre), _money(r.gains_post),
                _money(r.current_year_losses), _money(r.net_gain),
                _money(r.current_year_loss_carried),
                _money(r.brought_forward_available),
                _money(r.brought_forward_used),
                _money(r.annual_exempt_amount), _money(r.annual_exempt_used),
                _money(r.taxable_pre), _money(r.taxable_post),
                _money(r.taxable_total), _money(r.losses_carried_forward),
            ])
    return len(chain)


def _rate_gap_lines(gaps: list[RateGap]) -> list[str]:
    """Actionable missing-GBP-rate warnings: name the (currency, month)
    rows to add to the HMRC monthly-average CSV. Empty when there are no
    gaps. Amounts with no rate are excluded from the figures, so a gap
    means the report/forecast understates until the rate is supplied."""

    if not gaps:
        return []
    uniq = sorted(set(gaps), key=lambda g: (g.currency, g.month, g.isin))
    lines = [
        "WARN missing GBP rate — excluded from the figures above, so these "
        "understate until you add the month/currency to "
        "data/fx/hmrc-monthly-average.csv (or stamp the transaction's "
        "gbp-rate):"
    ]
    lines += [f"  {g.currency} {g.month} ({g.isin})" for g in uniq]
    return lines


def _partition_fig_relief(
    sa108: Sa108Report, sa106: Sa106Report, eri: EriResult
) -> tuple[
    Sa108Report,
    Sa106Report,
    EriResult,
    list[FigDesignationRow],
]:
    """Split foreign (FIG-relievable) items out of the SA schedules.

    Under a FIG claim, foreign income and non-UK gains move off SA108 /
    SA106 onto the FIG designation pages. Returns the UK-only schedules to
    file plus the relieved items as :class:`FigDesignationRow`s for the
    designation CSV. SA106 income is foreign by construction (UK income
    goes to SA100), so it's relieved in full. Disposal-derived rows are
    bucketed ``"gain"`` / ``"loss"`` by the sign of the gain so a
    forfeited foreign loss isn't netted away silently.
    """

    uk_rows = [r for r in sa108.rows if not r.is_foreign]
    uk_dds = [r for r in sa108.dds_disposals if not r.is_foreign]
    sa108_uk = Sa108Report(
        rows=uk_rows,
        dds_disposals=uk_dds,
        missing_rate_isins=sa108.missing_rate_isins,
        missing_rates=sa108.missing_rates,
        unmatched_isins=sa108.unmatched_isins,
    )
    sa106_uk = Sa106Report(
        dividends=[], interest=[], missing_rate_isins=sa106.missing_rate_isins,
        missing_rates=sa106.missing_rates,
    )
    eri_uk = EriResult(
        rows=[r for r in eri.rows if r.country == "GB"],
        base_cost_adjustments=eri.base_cost_adjustments,
        missing_rate_isins=eri.missing_rate_isins,
        missing_rates=eri.missing_rates,
    )

    designation: list[FigDesignationRow] = []
    for d in sa106.dividends:
        designation.append(
            FigDesignationRow(
                "income", "foreign dividend", d.country, d.isin,
                d.commodity_name, d.gross_gbp,
            )
        )
    for i in sa106.interest:
        designation.append(
            FigDesignationRow(
                "income", "foreign interest", i.country, i.isin,
                i.commodity_name, i.gross_gbp,
            )
        )
    for e in eri.rows:
        if e.country != "GB":
            designation.append(
                FigDesignationRow(
                    "income", f"ERI ({e.income_type})", e.country, e.isin,
                    e.commodity_name, e.gross_gbp,
                )
            )
    for r in sa108.rows:
        if r.is_foreign:
            category = (
                "offshore income gain"
                if r.reporting_status == "non-reporting"
                else "capital gain"
            )
            kind: FigKind = "gain" if r.gain_gbp >= 0 else "loss"
            designation.append(
                FigDesignationRow(
                    kind, category, r.isin[:2], r.isin, r.commodity_name,
                    r.gain_gbp,
                )
            )
    for r in sa108.dds_disposals:
        if r.is_foreign:
            kind = "gain" if r.gain_gbp >= 0 else "loss"
            designation.append(
                FigDesignationRow(
                    kind, "deep-discounted", r.isin[:2], r.isin,
                    r.commodity_name, r.gain_gbp,
                )
            )
    return sa108_uk, sa106_uk, eri_uk, designation


def _write_fig_designation_csv(path: Path, rows: list[FigDesignationRow]) -> int:
    """Write the foreign income / gains relieved under a FIG claim (the
    amounts to declare on the FIG pages). The ``kind`` column buckets each
    row as relieved income / relieved gain / disallowed loss so a forfeited
    foreign loss is visible rather than netted away. Returns the row count."""

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["kind", "category", "country", "isin", "name", "amount_gbp"]
        )
        for r in rows:
            writer.writerow([
                r.kind, r.category, r.country, r.isin, r.name,
                _money(r.amount_gbp),
            ])
    return len(rows)


def _write_tax_summary(
    path: Path,
    year: str,
    sa108: Sa108Report,
    sa106: Sa106Report,
    eri: EriResult,
    allowance: CgtAllowanceResult,
    rate_change_date: date | None = None,
    aea_missing: bool = False,
    fig_claimed: bool = False,
    fig_designation: list[FigDesignationRow] | None = None,
) -> None:
    cgt = [r for r in sa108.rows if r.reporting_status in CGT_STATUSES]
    offshore = [r for r in sa108.rows if r.reporting_status == "non-reporting"]
    unclassified = [r for r in sa108.rows if r.reporting_status == "unknown"]

    def _total(rows: list, attr: str) -> str:  # type: ignore[type-arg]
        return _money(sum((getattr(r, attr) for r in rows), Decimal(0)))

    def _gains(rows: list) -> str:  # type: ignore[type-arg]
        return _money(sum((r.gain_gbp for r in rows if r.gain_gbp > 0), Decimal(0)))

    losses = _money(sum((r.gain_gbp for r in cgt if r.gain_gbp < 0), Decimal(0)))

    lines = [
        f"UK tax report — {year}",
        "",
        "SA108 capital gains (reporting / uk-domestic):",
        f"  disposals: {len(cgt)}",
    ]
    if rate_change_date is not None:
        label = f"{rate_change_date.day} {rate_change_date:%B %Y}"
        lines += [
            f"  gains before {label}: {_gains([r for r in cgt if r.period == 'pre'])} GBP",
            f"  gains on/after {label}: {_gains([r for r in cgt if r.period == 'post'])} GBP",
        ]
    else:
        lines.append(f"  total gains: {_gains(cgt)} GBP")
    lines.append(f"  allowable losses (this year): {losses} GBP")
    lines.append("")
    lines.append("CGT allowances and loss relief:")
    lines.append(
        f"  net gain after current-year losses: {_money(allowance.net_gain)} GBP"
    )
    if allowance.current_year_loss_carried > 0:
        lines.append(
            "  current-year loss carried forward: "
            f"{_money(allowance.current_year_loss_carried)} GBP"
        )
    lines.append(
        "  brought-forward losses available: "
        f"{_money(allowance.brought_forward_available)} GBP"
    )
    lines.append(
        f"  brought-forward losses used: {_money(allowance.brought_forward_used)} GBP"
    )
    lines.append(
        f"  annual exempt amount: {_money(allowance.annual_exempt_amount)} GBP"
    )
    if allowance.rate_split and rate_change_date is not None:
        label = f"{rate_change_date.day} {rate_change_date:%B %Y}"
        lines.append(
            f"  taxable gain before {label}: {_money(allowance.taxable_pre)} GBP"
        )
        lines.append(
            f"  taxable gain on/after {label}: {_money(allowance.taxable_post)} GBP"
        )
        lines.append(f"  taxable gain (total): {_money(allowance.taxable_total)} GBP")
    else:
        lines.append(f"  taxable gain: {_money(allowance.taxable_total)} GBP")
    lines.append(
        "  losses carried forward to next year: "
        f"{_money(allowance.losses_carried_forward)} GBP"
    )
    if aea_missing:
        lines.append(
            f"  WARN no annual exempt amount configured for {year} — treated as "
            "0; add it to cgt_annual_exempt_amount."
        )
    lines += [
        "",
        "SA106 foreign dividends:",
        f"  groups: {len(sa106.dividends)}",
        f"  total gross: {_total(sa106.dividends, 'gross_gbp')} GBP",
        f"  total withholding tax: {_total(sa106.dividends, 'wht_gbp')} GBP",
        "",
    ]
    if sa106.interest:
        lines += [
            "SA106 foreign interest (bond-fund distributions):",
            f"  groups: {len(sa106.interest)}",
            f"  total gross: {_total(sa106.interest, 'gross_gbp')} GBP",
            f"  total withholding tax: {_total(sa106.interest, 'wht_gbp')} GBP",
            "",
        ]
    if eri.rows:
        eri_div = [r for r in eri.rows if r.income_type == "dividend"]
        eri_int = [r for r in eri.rows if r.income_type == "interest"]
        lines.append("SA106 excess reportable income (reporting funds):")
        lines.append(
            f"  dividend — taxable income: {_total(eri_div, 'gross_gbp')} GBP "
            f"(equalisation {_total(eri_div, 'equalisation_gbp')}, "
            f"base-cost uplift {_total(eri_div, 'base_cost_adjustment_gbp')})"
        )
        lines.append(
            f"  interest — taxable income: {_total(eri_int, 'gross_gbp')} GBP "
            f"(equalisation {_total(eri_int, 'equalisation_gbp')}, "
            f"base-cost uplift {_total(eri_int, 'base_cost_adjustment_gbp')})"
        )
        lines.append("")
    if offshore:
        lines.append(
            "SA106 offshore income gains (non-reporting funds):"
        )
        lines.append(f"  disposals: {len(offshore)}")
        lines.append(f"  total gain: {_total(offshore, 'gain_gbp')} GBP")
        lines.append("")
    if sa108.dds_disposals:
        dds_losses = _money(
            sum((r.gain_gbp for r in sa108.dds_disposals if r.gain_gbp < 0), Decimal(0))
        )
        lines.append("Deep discounted securities (taxed to income):")
        lines.append(f"  disposals: {len(sa108.dds_disposals)}")
        lines.append(f"  gains taxed to income: {_gains(sa108.dds_disposals)} GBP")
        lines.append(
            f"  securities losses (generally not allowable): {dds_losses} GBP"
        )
        lines.append("")
    if unclassified:
        isins = sorted({r.isin for r in unclassified})
        lines.append(
            "WARN_UNCLASSIFIED disposals with no commodity metadata "
            "(add entries to data/commodities.toml):"
        )
        for isin in isins:
            lines.append(f"  {isin}")
        if fig_claimed:
            lines.append(
                "  NOTE under the FIG claim these default to UK-situs and are "
                "neither taxed nor relieved; if any is actually foreign it is "
                "MISSING RELIEF — set its situs in data/commodities.toml "
                "before filing."
            )
        lines.append("")
    if allowance.expired_loss_claims:
        lines.append(
            "WARN_LOSS_CLAIM_WINDOW brought-forward losses relieved more than "
            "4 years after they arose — each must have been notified to HMRC "
            "by its deadline below, or it is not allowable for "
            f"{allowance.tax_year} (figures above are unadjusted; confirm "
            "before relying on them):"
        )
        for w in allowance.expired_loss_claims:
            lines.append(
                f"  loss from {w.arising_year} ({_money(w.amount_used)} GBP) "
                f"— notify-by deadline {w.deadline.day} {w.deadline:%b %Y}"
            )
        lines.append("")
    if sa108.unmatched_isins:
        lines.append(
            "WARN disposed more than acquired — add opening positions to "
            "data/opening-positions.toml (shortfall matched at zero cost):"
        )
        for isin in sa108.unmatched_isins:
            lines.append(f"  {isin}")
        lines.append("")
    gap_lines = _rate_gap_lines(
        sa108.missing_rates + sa106.missing_rates + eri.missing_rates
    )
    if gap_lines:
        lines += gap_lines
        lines.append("")
    if fig_claimed:
        sub = fig_subtotals(fig_designation or [])
        lines.append("Foreign Income & Gains (FIG) claim:")
        lines.append(f"  foreign income relieved: {_money(sub.income)} GBP")
        lines.append(f"  non-UK gains relieved: {_money(sub.gains)} GBP")
        lines.append(
            "  disallowed foreign losses (loss relief forfeited): "
            f"{_money(sub.losses)} GBP"
        )
        lines.append(
            f"  net foreign income + gains relieved: {_money(sub.net)} GBP "
            "(see fig-designation.csv)"
        )
        lines.append(
            "  the SA108 / SA106 figures above are UK-situs only; the "
            "personal allowance and CGT annual exempt amount are forfeited."
        )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


@app.command("tax-report")
def tax_report(
    year: Annotated[
        str,
        typer.Option("--year", help="UK tax year to report, e.g. 2025-26."),
    ],
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked (recursively) for *.transactions.jsonl "
            "sidecars. Defaults to ``data``.",
        ),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory for the CSVs. Defaults to "
            "``<tax_reports_dir>/<year>``.",
        ),
    ] = None,
    commodities: Annotated[
        Path | None,
        typer.Option(
            "--commodities",
            help="Commodity-metadata TOML. Defaults to the configured "
            "``commodities_metadata_path``.",
        ),
    ] = None,
    rate_source: Annotated[
        str | None,
        typer.Option(
            "--rate-source",
            help="GBP rate source for transactions not enriched at ingest "
            "(``null`` | ``hmrc-monthly``). Defaults to the configured "
            "source.",
        ),
    ] = None,
    opening_positions: Annotated[
        Path | None,
        typer.Option(
            "--opening-positions",
            help="Pre-ledger opening-positions TOML seeded into the "
            "section 104 pool. Defaults to the configured "
            "``opening_positions_path``.",
        ),
    ] = None,
    eri: Annotated[
        Path | None,
        typer.Option(
            "--eri",
            help="Excess reportable income TOML for accumulating "
            "reporting funds. Defaults to the configured ``eri_path``.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any amount couldn't be converted to GBP "
            "(a missing rate silently excludes it). Turn on for a CI gate.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Produce UK SA106 / SA108 CSV inputs from the JSONL sidecars.

    Reads the structured transaction sidecars (no beancount parsing),
    applies UK tax-year boundaries and section 104 / same-day / 30-day
    matching, and writes ``sa108-disposals.csv``,
    ``sa106-dividends.csv``, ``sa106-interest.csv`` (distributions from
    >60%-interest-bearing offshore funds, flagged via
    ``distributions_as_interest`` in commodities metadata),
    ``sa106-offshore-income-gains.csv``, ``sa106-deep-discounted.csv``,
    ``sa106-eri.csv`` (excess reportable income, which also uplifts the
    CGT base cost), ``cgt-loss-carryforward.csv`` (the year-by-year annual
    exempt amount + allowable-loss chain) and ``summary.txt``.
    Current-account interest is loan interest the user pays (an expense),
    so it isn't foreign income; reporting-fund accumulated interest
    arrives via ERI.
    """

    _configure_logging(verbose)
    tax_year_bounds(year)  # validate the label early

    arrival = settings.uk_residence_start_date
    if is_pre_residence_year(year, arrival):
        err_console.print(
            f"{year} is before UK residence began ({arrival}); foreign income "
            "and gains aren't UK-taxable while non-resident — nothing to report."
        )
        return
    for bad in ineligible_claims(settings.fig_claim_years, arrival):
        err_console.print(
            f"WARN FIG claim for {bad} is outside the eligible window "
            f"{sorted(fig_eligible_years(arrival))}; relief still applied as "
            "configured."
        )

    out_dir = out if out is not None else settings.tax_reports_dir / year
    comp = _compute_tax_year(
        year=year, source=source, commodities=commodities,
        rate_source=rate_source, opening_positions=opening_positions, eri=eri,
    )
    sa108, sa106, eri_result = comp.sa108, comp.sa106, comp.eri_result
    fig_claimed = comp.fig_claimed

    # CGT annual exempt amount + loss carry-forward: thread allowable
    # losses across tax years to the requested one (residence- and
    # FIG-aware), seeded by any pre-ledger brought-forward losses.
    chain = loss_carryforward_chain(
        comp.history.rows,
        through_year=year,
        aea_by_year=settings.cgt_annual_exempt_amount,
        rate_change_dates=settings.cgt_rate_change_dates,
        pre_ledger_losses=comp.pre_ledger_losses,
        arrival=arrival,
        fig_claim_years=comp.fig_claim_years,
    )
    allowance = chain[year]
    aea_missing = year not in settings.cgt_annual_exempt_amount

    # Under a FIG claim, foreign income and non-UK gains move off the SA
    # schedules onto the FIG designation pages; only UK-situs items remain.
    designation: list[FigDesignationRow] = []
    if fig_claimed:
        sa108, sa106, eri_result, designation = _partition_fig_relief(
            sa108, sa106, eri_result
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    n_cgt = _write_sa108_csv(out_dir / "sa108-disposals.csv", sa108)
    n_div = _write_sa106_dividends_csv(out_dir / "sa106-dividends.csv", sa106)
    n_int = _write_sa106_interest_csv(out_dir / "sa106-interest.csv", sa106)
    n_oig = _write_offshore_income_gains_csv(
        out_dir / "sa106-offshore-income-gains.csv", sa108
    )
    n_dds = _write_deep_discounted_csv(
        out_dir / "sa106-deep-discounted.csv", sa108
    )
    n_eri = _write_eri_csv(out_dir / "sa106-eri.csv", eri_result)
    _write_cgt_carryforward_csv(out_dir / "cgt-loss-carryforward.csv", chain)
    if fig_claimed:
        _write_fig_designation_csv(out_dir / "fig-designation.csv", designation)
    _write_tax_summary(
        out_dir / "summary.txt", year, sa108, sa106, eri_result, allowance,
        rate_change_date=settings.cgt_rate_change_dates.get(year),
        aea_missing=aea_missing,
        fig_claimed=fig_claimed,
        fig_designation=designation,
    )

    fig_note = (
        f", {len(designation)} FIG-relieved item(s)" if fig_claimed else ""
    )
    err_console.print(
        f"Wrote tax report for {year} to {out_dir} "
        f"({n_cgt} SA108 disposal(s), {n_div} SA106 dividend group(s), "
        f"{n_int} SA106 interest group(s), "
        f"{n_oig} offshore income gain(s), {n_dds} deep-discounted disposal(s), "
        f"{n_eri} ERI group(s){fig_note})"
    )

    gaps = sa108.missing_rates + sa106.missing_rates + eri_result.missing_rates
    if gaps:
        for line in _rate_gap_lines(gaps):
            err_console.print(line)
        if strict:
            raise typer.Exit(code=1)


# --- tax-forecast -----------------------------------------------------------

def _positive_gains(rows: list[Sa108Row]) -> Decimal:
    """Sum the positive gains in ``rows`` (losses on income-charged
    disposals — offshore funds, deeply discounted securities — are not
    relievable against income, so they're dropped, not netted)."""

    return sum((r.gain_gbp for r in rows if r.gain_gbp > 0), Decimal(0))


def _resolve_year_rates(year: str) -> tuple[IncomeTaxBands, CgtRateSchedule]:
    """The statutory income bands + CGT rates for ``year``, or exit with a
    clear message if either is unconfigured (rather than guessing)."""

    bands = settings.income_tax_bands.get(year)
    if bands is None:
        err_console.print(
            f"No income-tax bands configured for {year}; add it to "
            "income_tax_bands (see tax/uk/rates.py)."
        )
        raise typer.Exit(code=1)
    cgt_rates = settings.cgt_forecast_rates.get(year)
    if cgt_rates is None:
        err_console.print(
            f"No CGT rates configured for {year}; add it to "
            "cgt_forecast_rates (see tax/uk/rates.py)."
        )
        raise typer.Exit(code=1)
    return bands, cgt_rates


def _fig_year_inputs(
    comp: _TaxComputation, *, year: str, other_income: Decimal
) -> FigYearInputs:
    """Build the liability inputs for ``year`` from its computed reports:
    the income-charged gains split by situs (foreign relievable under a
    FIG claim, UK always taxed) and the SA106 + ERI dividend/interest."""

    bands, cgt_rates = _resolve_year_rates(year)
    income_gain_rows = [
        r for r in comp.sa108.rows if r.reporting_status == "non-reporting"
    ] + comp.sa108.dds_disposals
    eri_div = sum(
        (r.gross_gbp for r in comp.eri_result.rows if r.income_type == "dividend"),
        Decimal(0),
    )
    eri_int = sum(
        (r.gross_gbp for r in comp.eri_result.rows if r.income_type == "interest"),
        Decimal(0),
    )
    return FigYearInputs(
        year=year,
        other_income=other_income,
        uk_other=_positive_gains([r for r in income_gain_rows if not r.is_foreign]),
        foreign_other=_positive_gains([r for r in income_gain_rows if r.is_foreign]),
        dividend_income=sum((r.gross_gbp for r in comp.sa106.dividends), Decimal(0))
        + eri_div,
        dividend_wht=sum((r.wht_gbp for r in comp.sa106.dividends), Decimal(0)),
        interest_income=sum((r.gross_gbp for r in comp.sa106.interest), Decimal(0))
        + eri_int,
        interest_wht=sum((r.wht_gbp for r in comp.sa106.interest), Decimal(0)),
        bands=bands,
        cgt_rates=cgt_rates,
    )


def _write_forecast_csv(path: Path, liab: LiabilityResult) -> None:
    """One row per liability component, plus a TOTAL row."""

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["component", "taxable_gbp", "tax_gbp"])
        writer.writerow(
            ["non-savings income", _money(liab.nonsavings_taxable),
             _money(liab.nonsavings_tax)]
        )
        writer.writerow(
            ["foreign interest", _money(liab.interest_taxable),
             _money(liab.interest_tax)]
        )
        writer.writerow(
            ["foreign dividends", _money(liab.dividend_taxable),
             _money(liab.dividend_tax)]
        )
        writer.writerow(
            ["foreign tax credit relief", "",
             _money(-liab.foreign_tax_credit)]
        )
        writer.writerow(
            ["capital gains", _money(liab.cgt_taxable_pre + liab.cgt_taxable_post),
             _money(liab.cgt_tax)]
        )
        writer.writerow(["TOTAL", "", _money(liab.total_liability)])


def _write_forecast_summary(
    path: Path,
    liab: LiabilityResult,
    *,
    as_of: date,
    alt: LiabilityResult | None = None,
    recommendation: str | None = None,
    rate_gaps: list[RateGap] | None = None,
) -> None:
    m = _money
    lines = [
        f"UK tax-liability forecast — {liab.tax_year}",
        f"(year-to-date actuals as of {as_of.isoformat()}; an estimate, "
        "not a return)",
        "",
        "Assumed income:",
        f"  expected non-savings income: {m(liab.other_income)} GBP",
    ]
    if liab.other_taxable_income > 0:
        lines.append(
            "  income-charged investment profit (offshore / deep-discounted): "
            f"{m(liab.other_taxable_income)} GBP"
        )
    if liab.fig_claimed:
        lines.append(
            "  FIG claim: foreign income + non-UK gains relieved "
            f"({m(liab.relieved_income)} GBP income relieved; personal "
            "allowance and CGT annual exempt amount forfeited)"
        )
    lines += [
        f"  personal allowance (after taper): {m(liab.personal_allowance)} GBP",
        "",
        "Income tax:",
        f"  non-savings taxable: {m(liab.nonsavings_taxable)} GBP "
        f"→ tax {m(liab.nonsavings_tax)} GBP",
        f"  foreign interest: {m(liab.interest_income)} GBP "
        f"(starting-rate band {m(liab.starting_rate_used)}, "
        f"PSA {m(liab.psa_used)}, taxable {m(liab.interest_taxable)}) "
        f"→ tax {m(liab.interest_tax)} GBP",
        f"  foreign dividends: {m(liab.dividend_income)} GBP "
        f"(allowance {m(liab.dividend_allowance_used)}, "
        f"taxable {m(liab.dividend_taxable)}) → tax {m(liab.dividend_tax)} GBP",
        f"  income tax before relief: {m(liab.income_tax_before_ftcr)} GBP",
        f"  foreign tax credit relief: {m(liab.foreign_tax_credit)} GBP "
        f"(interest {m(liab.interest_ftcr)}, dividend {m(liab.dividend_ftcr)})",
        f"  income tax: {m(liab.income_tax)} GBP",
        "",
        "Capital gains tax:",
        f"  taxable gain (after AEA + losses): "
        f"{m(liab.cgt_taxable_pre + liab.cgt_taxable_post)} GBP",
        f"  basic-rate band remaining for gains: "
        f"{m(liab.cgt_basic_band_remaining)} GBP",
        f"  taxed at lower rate: {m(liab.cgt_at_lower)} GBP, "
        f"higher rate: {m(liab.cgt_at_higher)} GBP",
        f"  capital gains tax: {m(liab.cgt_tax)} GBP",
        "",
        f"ESTIMATED TOTAL LIABILITY: {m(liab.total_liability)} GBP",
    ]
    if alt is not None and recommendation is not None:
        claim = liab if liab.fig_claimed else alt
        noclaim = alt if liab.fig_claimed else liab
        saving = abs(claim.total_liability - noclaim.total_liability)
        lines += [
            "",
            "FIG claim decision (this year is FIG-eligible):",
            f"  with claim:    {m(claim.total_liability)} GBP",
            f"  without claim: {m(noclaim.total_liability)} GBP",
            f"  RECOMMENDED: {recommendation} (saves {m(saving)} GBP) — the "
            "claim is elective; set fig_claim_years to apply it.",
        ]
    gap_lines = _rate_gap_lines(rate_gaps or [])
    if gap_lines:
        lines.append("")
        lines += gap_lines
    lines += [
        "",
        "Assumes England/Wales/NI rates and a single taxpayer; excludes "
        "PAYE/payments already made, pension/gift-aid relief, and the "
        "marriage allowance.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


@app.command("tax-forecast")
def tax_forecast(
    income: Annotated[
        str,
        typer.Option(
            "--income",
            help="Expected non-savings, non-dividend taxable income for the "
            "year (e.g. salary + rent), before the personal allowance. Sets "
            "the marginal band the investment income/gains stack on top of.",
        ),
    ],
    year: Annotated[
        str | None,
        typer.Option(
            "--year",
            help="UK tax year to forecast, e.g. 2026-27. Defaults to the "
            "current (incomplete) tax year.",
        ),
    ] = None,
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked (recursively) for *.transactions.jsonl "
            "sidecars. Defaults to ``data``.",
        ),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to ``<tax_reports_dir>/<year>``.",
        ),
    ] = None,
    commodities: Annotated[
        Path | None,
        typer.Option("--commodities", help="Commodity-metadata TOML."),
    ] = None,
    rate_source: Annotated[
        str | None,
        typer.Option("--rate-source", help="GBP rate source override."),
    ] = None,
    opening_positions: Annotated[
        Path | None,
        typer.Option("--opening-positions", help="Opening-positions TOML."),
    ] = None,
    eri: Annotated[
        Path | None,
        typer.Option("--eri", help="Excess reportable income TOML."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any amount couldn't be converted to GBP "
            "(a missing rate silently understates the estimate).",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Estimate this tax year's UK liability so April holds no surprises.

    Reuses the SA108/SA106 machinery (``tax-report``) to compute the
    year-to-date taxable amounts, then stacks them in UK order —
    non-savings income, savings, dividends, then capital gains on top —
    and applies the statutory rates/bands to produce an estimated
    liability. Reports year-to-date *actuals* only (no run-rate
    extrapolation), and writes ``forecast-summary.txt`` +
    ``forecast.csv``. Foreign withholding tax is credited against the UK
    tax on that income. ISA-wrapped transactions are excluded, as for
    ``tax-report``.
    """

    _configure_logging(verbose)
    today = date.today()
    year = year or date_to_tax_year(today)
    tax_year_bounds(year)  # validate the label early

    arrival = settings.uk_residence_start_date
    if is_pre_residence_year(year, arrival):
        err_console.print(
            f"{year} is before UK residence began ({arrival}); no UK liability "
            "while non-resident — nothing to forecast."
        )
        return
    for bad in ineligible_claims(settings.fig_claim_years, arrival):
        err_console.print(
            f"WARN FIG claim for {bad} is outside the eligible window "
            f"{sorted(fig_eligible_years(arrival))}; relief still applied as "
            "configured."
        )

    try:
        expected_income = Decimal(income)
    except (ArithmeticError, ValueError):
        err_console.print(f"--income must be a number, got {income!r}.")
        raise typer.Exit(code=1) from None

    out_dir = out if out is not None else settings.tax_reports_dir / year
    comp = _compute_tax_year(
        year=year, source=source, commodities=commodities,
        rate_source=rate_source, opening_positions=opening_positions, eri=eri,
    )
    inp = _fig_year_inputs(comp, year=year, other_income=expected_income)

    def _run_scenario(claim_this_year: bool) -> LiabilityResult:
        years = (
            settings.fig_claim_years | {year}
            if claim_this_year
            else settings.fig_claim_years - {year}
        )
        chain = loss_carryforward_chain(
            comp.history.rows, through_year=year,
            aea_by_year=settings.cgt_annual_exempt_amount,
            rate_change_dates=settings.cgt_rate_change_dates,
            pre_ledger_losses=comp.pre_ledger_losses,
            arrival=arrival, fig_claim_years=years,
        )
        allowance = chain[year]
        return compute_liability(
            tax_year=year,
            other_income=inp.other_income,
            other_taxable_income=inp.uk_other,
            foreign_other_income=inp.foreign_other,
            interest_income=inp.interest_income,
            interest_wht=inp.interest_wht,
            dividend_income=inp.dividend_income,
            dividend_wht=inp.dividend_wht,
            cgt_taxable_pre=allowance.taxable_pre,
            cgt_taxable_post=allowance.taxable_post,
            bands=inp.bands,
            cgt_rates=inp.cgt_rates,
            fig_claimed=claim_this_year,
        )

    # When the year is FIG-eligible, the claim is elective — compute both
    # ways and recommend the cheaper. Otherwise honour the configured
    # claim set (normally no claim).
    eligible = year in fig_eligible_years(arrival)
    alt: LiabilityResult | None = None
    recommendation: str | None = None
    if eligible:
        no_claim = _run_scenario(False)
        with_claim = _run_scenario(True)
        if with_claim.total_liability < no_claim.total_liability:
            liab, alt, recommendation = with_claim, no_claim, "claim"
        else:
            liab, alt, recommendation = no_claim, with_claim, "no claim"
    else:
        liab = _run_scenario(year in settings.fig_claim_years)

    gaps = comp.rate_gaps

    out_dir.mkdir(parents=True, exist_ok=True)
    as_of = today if date_to_tax_year(today) == year else tax_year_bounds(year)[1]
    _write_forecast_summary(
        out_dir / "forecast-summary.txt", liab, as_of=as_of,
        alt=alt, recommendation=recommendation, rate_gaps=gaps,
    )
    _write_forecast_csv(out_dir / "forecast.csv", liab)

    rec = f"; recommended: {recommendation}" if recommendation else ""
    err_console.print(
        f"Wrote tax-liability forecast for {year} to {out_dir} "
        f"(estimated total {_money(liab.total_liability)} GBP{rec})"
    )
    if gaps:
        for line in _rate_gap_lines(gaps):
            err_console.print(line)
        if strict:
            raise typer.Exit(code=1)


# --- tax-pack ---------------------------------------------------------------

@app.command("tax-pack")
def tax_pack(
    year: Annotated[
        str | None,
        typer.Option(
            "--year",
            help="UK tax year, e.g. 2025-26. Defaults to the current "
            "(in-progress) tax year.",
        ),
    ] = None,
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked (recursively) for *.transactions.jsonl "
            "sidecars. Defaults to ``data``.",
        ),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to ``<tax_reports_dir>/<year>``.",
        ),
    ] = None,
    commodities: Annotated[
        Path | None,
        typer.Option("--commodities", help="Commodity-metadata TOML."),
    ] = None,
    rate_source: Annotated[
        str | None,
        typer.Option("--rate-source", help="GBP rate source override."),
    ] = None,
    opening_positions: Annotated[
        Path | None,
        typer.Option("--opening-positions", help="Opening-positions TOML."),
    ] = None,
    eri: Annotated[
        Path | None,
        typer.Option("--eri", help="Excess reportable income TOML."),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Render a per-year 'tax pack' — one Markdown filing aid that ties the
    computed SA108 / SA106 figures to the boxes on the HMRC forms.

    Computes the same year-to-date figures as ``tax-report`` (section 104
    matching, foreign income, the CGT allowance chain, residence + FIG
    treatment) and writes ``tax-pack.md``. A filing aid, not tax advice;
    box numbers are indicative and must be verified against the year's
    form. ISA-wrapped transactions are excluded.
    """

    _configure_logging(verbose)
    today = date.today()
    year = year or date_to_tax_year(today)
    tax_year_bounds(year)  # validate the label early

    arrival = settings.uk_residence_start_date
    if is_pre_residence_year(year, arrival):
        err_console.print(
            f"{year} is before UK residence began ({arrival}); foreign income "
            "and gains aren't UK-taxable while non-resident — nothing to pack."
        )
        return

    out_dir = out if out is not None else settings.tax_reports_dir / year
    comp = _compute_tax_year(
        year=year, source=source, commodities=commodities,
        rate_source=rate_source, opening_positions=opening_positions, eri=eri,
    )
    sa108, sa106, eri_result = comp.sa108, comp.sa106, comp.eri_result
    fig_claimed = comp.fig_claimed

    chain = loss_carryforward_chain(
        comp.history.rows, through_year=year,
        aea_by_year=settings.cgt_annual_exempt_amount,
        rate_change_dates=settings.cgt_rate_change_dates,
        pre_ledger_losses=comp.pre_ledger_losses,
        arrival=arrival, fig_claim_years=comp.fig_claim_years,
    )
    allowance = chain[year]

    gaps = comp.rate_gaps
    designation: list[FigDesignationRow] = []
    if fig_claimed:
        sa108, sa106, eri_result, designation = _partition_fig_relief(
            sa108, sa106, eri_result
        )

    markdown = render_tax_pack(
        year=year,
        sa108=sa108,
        sa106=sa106,
        eri=eri_result,
        allowance=allowance,
        designation=designation,
        fig_claimed=fig_claimed,
        rate_change_date=settings.cgt_rate_change_dates.get(year),
        rate_gaps=gaps,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tax-pack.md").write_text(markdown, encoding="utf-8")
    err_console.print(f"Wrote tax pack for {year} to {out_dir / 'tax-pack.md'}")


# --- fig-advice -------------------------------------------------------------

def _fmt_claim(claimed: frozenset[str]) -> str:
    return ", ".join(sorted(claimed)) if claimed else "none"


def _write_fig_advice(
    path: Path,
    patterns: list[FigPattern],
    *,
    window: list[str],
    year_inputs: dict[str, FigYearInputs],
    as_of: date,
    rate_gaps: list[RateGap],
) -> None:
    m = _money
    recommended = patterns[0]
    by_claim = {p.claimed: p for p in patterns}
    none = by_claim[frozenset()]
    all_years = by_claim[frozenset(window)]
    income = year_inputs[window[0]].other_income

    lines = [
        "UK FIG claim advice",
        f"(year-to-date actuals as of {as_of.isoformat()}; an estimate, "
        "not tax advice)",
        "",
        f"Eligible FIG window: {', '.join(window)}",
        f"Assumed non-savings income each year: {m(income)} GBP",
        "",
        "A claim relieves that year's foreign income and non-UK gains, but "
        "forfeits its personal allowance and CGT annual exempt amount AND "
        "disallows that year's foreign losses (which would otherwise carry "
        "forward). Foreign income relievable per year:",
        "",
    ]
    for y in window:
        inp = year_inputs[y]
        foreign_income = inp.dividend_income + inp.interest_income + inp.foreign_other
        lines.append(
            f"  {y}: foreign income {m(foreign_income)} GBP "
            f"(+ that year's foreign capital gains)"
        )
    lines += [
        "",
        "Total liability across the window by claim pattern "
        "(cheapest first):",
        "",
    ]
    for p in patterns:
        marker = "  → " if p.claimed == recommended.claimed else "    "
        lines.append(
            f"{marker}claim [{_fmt_claim(p.claimed)}]: "
            f"{m(p.total_liability)} GBP"
        )
    lines += [
        "",
        f"RECOMMENDED: claim [{_fmt_claim(recommended.claimed)}] "
        f"— {m(recommended.total_liability)} GBP across the window.",
        f"  vs claim none: saves {m(none.total_liability - recommended.total_liability)} GBP",
        f"  vs claim all:  saves {m(all_years.total_liability - recommended.total_liability)} GBP",
        "",
        "Per-year liability under the recommended pattern:",
    ]
    for y in window:
        claimed = "claimed" if y in recommended.claimed else "not claimed"
        lines.append(f"  {y} ({claimed}): {m(recommended.per_year[y])} GBP")

    gap_lines = _rate_gap_lines(rate_gaps)
    if gap_lines:
        lines += ["", *gap_lines]
    lines += [
        "",
        "Caveats: incomplete years use year-to-date actuals, so any "
        "recommendation touching the current year is provisional — re-run "
        "as statements land. Assumes the same income each year, "
        "England/Wales/NI rates, a single taxpayer. Not tax advice; the "
        "4-year-window eligibility (10 prior non-resident years) is "
        "asserted by your configured arrival date.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


@app.command("fig-advice")
def fig_advice(
    income: Annotated[
        str,
        typer.Option(
            "--income",
            help="Expected non-savings income each window year (before the "
            "personal allowance). Applied to every eligible year.",
        ),
    ],
    source: Annotated[
        Path,
        typer.Option("--source", help="Sidecar directory. Defaults to ``data``."),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to ``tax_reports_dir`` (the "
            "advice spans the whole window, not a single year).",
        ),
    ] = None,
    commodities: Annotated[
        Path | None,
        typer.Option("--commodities", help="Commodity-metadata TOML."),
    ] = None,
    rate_source: Annotated[
        str | None,
        typer.Option("--rate-source", help="GBP rate source override."),
    ] = None,
    opening_positions: Annotated[
        Path | None,
        typer.Option("--opening-positions", help="Opening-positions TOML."),
    ] = None,
    eri: Annotated[
        Path | None,
        typer.Option("--eri", help="Excess reportable income TOML."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any amount lacked a GBP rate.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Recommend which FIG years to claim, optimised across the window.

    A FIG claim is decided per year but the years interact through the
    loss-carry-forward chain — claiming forfeits a year's allowable
    foreign losses as well as its allowances. This evaluates every claim
    combination over the eligible window jointly (year-to-date actuals)
    and recommends the cheapest, writing ``fig-advice.txt``. A planning
    aid, not tax advice; the current year's figures are provisional.
    """

    _configure_logging(verbose)
    today = date.today()
    arrival = settings.uk_residence_start_date
    eligible = sorted(fig_eligible_years(arrival))
    if not eligible:
        err_console.print(
            "No FIG window — set BANKPIPE_UK_RESIDENCE_START_DATE to a date "
            "in or after the 2021-22 tax year (the regime runs for the first "
            "four UK-resident years, from 2025-26)."
        )
        raise typer.Exit(code=1)
    # Later eligible years may not have statutory rates published yet; omit
    # them (they have no data anyway) rather than failing the whole run.
    window = [
        y for y in eligible
        if y in settings.income_tax_bands and y in settings.cgt_forecast_rates
    ]
    omitted = [y for y in eligible if y not in window]
    if omitted:
        err_console.print(
            f"Note: eligible year(s) {', '.join(omitted)} omitted — no "
            "statutory rates configured yet (add them to tax/uk/rates.py as "
            "HMRC sets them)."
        )
    if not window:
        err_console.print(
            "No eligible FIG year has configured rates yet — add the years to "
            "tax/uk/rates.py."
        )
        raise typer.Exit(code=1)

    try:
        expected_income = Decimal(income)
    except (ArithmeticError, ValueError):
        err_console.print(f"--income must be a number, got {income!r}.")
        raise typer.Exit(code=1) from None

    out_dir = out if out is not None else settings.tax_reports_dir
    year_inputs: dict[str, FigYearInputs] = {}
    rate_gaps: list[RateGap] = []
    history_rows: list[Sa108Row] = []
    pre_ledger_losses = Decimal(0)
    for y in window:
        comp = _compute_tax_year(
            year=y, source=source, commodities=commodities,
            rate_source=rate_source, opening_positions=opening_positions, eri=eri,
        )
        year_inputs[y] = _fig_year_inputs(comp, year=y, other_income=expected_income)
        rate_gaps += comp.rate_gaps
        # The matched history + brought-forward losses are year-independent.
        history_rows = comp.history.rows
        pre_ledger_losses = comp.pre_ledger_losses

    patterns = evaluate_fig_window(
        window=window,
        year_inputs=year_inputs,
        history_rows=history_rows,
        aea_by_year=settings.cgt_annual_exempt_amount,
        rate_change_dates=settings.cgt_rate_change_dates,
        pre_ledger_losses=pre_ledger_losses,
        arrival=arrival,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_fig_advice(
        out_dir / "fig-advice.txt", patterns,
        window=window, year_inputs=year_inputs, as_of=today, rate_gaps=rate_gaps,
    )
    recommended = patterns[0]
    err_console.print(
        f"Wrote FIG claim advice to {out_dir / 'fig-advice.txt'} "
        f"(recommended: claim [{_fmt_claim(recommended.claimed)}])"
    )
    if rate_gaps:
        for line in _rate_gap_lines(rate_gaps):
            err_console.print(line)
        if strict:
            raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
