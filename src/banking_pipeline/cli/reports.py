"""Analytical report commands.

The read-only Markdown/CSV reports: ``concentration``, ``net-worth``,
``allocation``, ``portfolio-allocation`` (the statement-valuation family)
and ``income`` (sidecar-driven). Each is a thin CLI wrapper over a report
module in :mod:`banking_pipeline`; the shared statement-loading helpers
live in :mod:`banking_pipeline.cli._main`.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, cast

import typer

from banking_pipeline import allocation as allocation_mod
from banking_pipeline import balance_sheet as balance_sheet_mod
from banking_pipeline import concentration as concentration_mod
from banking_pipeline import holdings as holdings_mod
from banking_pipeline import income as income_mod
from banking_pipeline import mandate_benchmark as mandate_benchmark_mod
from banking_pipeline import mandate_returns as mandate_returns_mod
from banking_pipeline import mandate_scorecard as mandate_scorecard_mod
from banking_pipeline import net_worth as net_worth_mod
from banking_pipeline import portfolio_allocation as portfolio_allocation_mod
from banking_pipeline import trial_balance as trial_balance_mod
from banking_pipeline.cli._main import (
    _configure_logging,
    _load_properties,
    _load_sidecar_transactions,
    _load_statement_context,
    _run_completeness,
    app,
    err_console,
)
from banking_pipeline.cli_options import (
    CommoditiesOpt,
    PropertyOpt,
    StatementOpt,
    StatementsDirOpt,
    StatementsRecursiveOpt,
    ValuationRateSourceOpt,
    VerboseOpt,
)
from banking_pipeline.commodities_metadata import load_commodities
from banking_pipeline.config import settings
from banking_pipeline.fx.gbp_rates import build_rate_source
from banking_pipeline.opening_positions import load_opening_positions
from banking_pipeline.tax.uk.basis import UkSection104Lens
from banking_pipeline.tax.uk.eri import cumulative_base_cost_adjustments, load_eri


@app.command()
def concentration(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``concentration_reports_dir`` (``reports/concentration``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    property_source: PropertyOpt = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any holding can't be valued (no "
            "statement mark or no GBP rate).",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Portfolio concentration / exposure breakdown.

    Reads the latest statement valuation per portfolio and breaks the
    total down by holding, asset class, currency, and domicile, writing
    ``concentration.md`` + ``holdings.csv``. Off-ledger residential
    property (``property.toml``) is folded in. A reporting aid: values are
    the statement marks converted to GBP at the configured rate.
    """

    _configure_logging(verbose)
    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    report = concentration_mod.build_report(
        texts, commodities=commodities_map, rate_source=rates,
        properties=_load_properties(property_source),
    )

    out_dir = out or settings.concentration_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "concentration.md").write_text(
        concentration_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "holdings.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(concentration_mod.render_csv_rows(report))

    err_console.print(
        f"Wrote concentration report to {out_dir} "
        f"({len(report.securities)} holding(s), gross long "
        f"£{report.gross_long_gbp:,.2f}, net worth "
        f"£{report.net_worth_gbp:,.2f})"
    )
    gap_n = len(report.missing_prices) + len(report.rate_gaps)
    if gap_n:
        err_console.print(
            f"[yellow]{gap_n} holding(s) excluded (no mark / no GBP "
            "rate) — see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command()
def holdings(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked recursively for *.transactions.jsonl "
            "sidecars — the cost-basis substrate (the same the tax reports "
            "read). Defaults to ``data``.",
        ),
    ] = Path("data"),
    basis: Annotated[
        str,
        typer.Option(
            "--basis",
            help="Cost-basis jurisdiction lens: ``uk`` (section 104, GBP). "
            "``es`` (EUR/Spanish FIFO) is reserved but not yet implemented.",
        ),
    ] = "uk",
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``holdings_reports_dir`` (``reports/holdings``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    opening_positions: Annotated[
        Path | None,
        typer.Option(
            "--opening-positions",
            help="Opening-positions TOML (pre-ledger lots seeding the section "
            "104 pool). Defaults to the configured ``opening_positions_path``.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any holding can't be valued (no statement "
            "mark or no GBP rate).",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Holdings cost basis + unrealised P&L.

    Joins the latest statement valuation per portfolio (market value, GBP)
    with a UK section 104 cost basis derived from the sidecars, and writes
    ``holdings.md`` + ``holdings.csv``. Cross-checks the statement quantity
    against the pool quantity. Cost basis is a UK-tax lens — not Pictet's
    EUR/Spanish figures, and not fed to the tax pipeline. A reporting aid,
    not advice.
    """

    _configure_logging(verbose)
    if basis == "es":
        err_console.print(
            "[red]error:[/red] --basis es is not yet implemented (blocks on "
            "the Pictet P&L parser — see the holdings-cost-basis-report plan)."
        )
        raise typer.Exit(code=2)
    if basis != "uk":
        err_console.print("[red]error:[/red] --basis must be 'uk' or 'es'")
        raise typer.Exit(code=2)

    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    # ISA-wrapped trades are UK-tax-exempt: they have no section 104 basis, so
    # they're excluded from the lens (mirrors the tax choke point). ISA
    # holdings still appear in the report from the statement side, with a
    # blank cost. ``rates`` is the exact-month source the tax pipeline uses
    # for cost conversion; ``value_holdings`` forward-fills it for the mark.
    txns = [tx for tx in _load_sidecar_transactions(source) if not tx.is_tax_exempt]
    opening_path = opening_positions or settings.opening_positions_path
    opening = (
        load_opening_positions(opening_path)
        if opening_path is not None and opening_path.is_file()
        else {}
    )
    # ERI base-cost uplift (excess reportable income already taxed) raises the
    # section 104 pool cost — accumulated across the whole history, since the
    # pool is cumulative and this is a current cost basis.
    eri_path = settings.eri_path
    eri_entries = (
        load_eri(eri_path) if eri_path is not None and eri_path.is_file() else {}
    )
    adjustments, eri_gaps = cumulative_base_cost_adjustments(
        txns, eri_entries=eri_entries, commodities=commodities_map,
        source=rates, opening_positions=opening,
    )
    lens = UkSection104Lens(
        transactions=txns, commodities=commodities_map, source=rates,
        opening_positions=opening, cost_adjustments=adjustments,
    )
    report = holdings_mod.build_report(
        texts, commodities=commodities_map, rate_source=rates, basis=lens,
        transactions=txns,
    )
    if eri_gaps:
        err_console.print(
            f"[yellow]{len(eri_gaps)} ERI entry/entries had no GBP rate — the "
            "cost basis for those holdings omits that uplift.[/yellow]"
        )

    out_dir = out or settings.holdings_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "holdings.md").write_text(
        holdings_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "holdings.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(holdings_mod.render_csv_rows(report))

    err_console.print(
        f"Wrote holdings report to {out_dir} ({len(report.rows)} holding(s), "
        f"market £{report.total_market_gbp:,.2f}, unrealised "
        f"£{report.total_unrealised_gbp:,.2f})"
    )
    if report.qty_drifts:
        n_gap = sum(1 for d in report.qty_drifts if d.kind == "gap")
        n_timing = len(report.qty_drifts) - n_gap
        err_console.print(
            f"[yellow]{len(report.qty_drifts)} holding(s) with statement/pool "
            f"quantity drift ({n_timing} timing, {n_gap} gap) — see the "
            "report.[/yellow]"
        )
    if report.unmatched_basis:
        n_gap = sum(1 for k in report.unmatched_basis
                    if report.unmatched_kind.get(k) == "gap")
        n_timing = len(report.unmatched_basis) - n_gap
        err_console.print(
            f"[yellow]{len(report.unmatched_basis)} holding(s) held per ledger "
            f"but not on the latest statement ({n_timing} timing, {n_gap} gap) "
            "— see the report.[/yellow]"
        )
    gap_n = len(report.missing_prices) + len(report.rate_gaps)
    if gap_n:
        err_console.print(
            f"[yellow]{gap_n} holding(s) excluded (no mark / no GBP "
            "rate) — see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command("net-worth")
def net_worth(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``net_worth_reports_dir`` (``reports/net-worth``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    property_source: PropertyOpt = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any snapshot couldn't be fully valued "
            "(no statement mark or no GBP rate), so a point understates.",
        ),
    ] = False,
    monthly: Annotated[
        bool,
        typer.Option(
            "--monthly",
            help="Resample onto a first-of-month grid instead of one row per "
            "raw statement date — drops the mid-month rows where only the ISA "
            "or a property valuation refreshed.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Net worth over time.

    Values each statement's valuation at its own date and builds a combined
    timeline across portfolios (each contributes its latest valuation on or
    before each date), writing ``net-worth.md`` + ``net-worth.csv``.
    Off-ledger residential property (``property.toml``) is folded in. A
    reporting aid: values are statement marks converted to GBP.
    """

    _configure_logging(verbose)
    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    timeline = net_worth_mod.build_timeline(
        texts, commodities=commodities_map, rate_source=rates,
        properties=_load_properties(property_source), monthly=monthly,
    )

    out_dir = out or settings.net_worth_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "net-worth.md").write_text(
        net_worth_mod.render_markdown(timeline), encoding="utf-8"
    )
    with (out_dir / "net-worth.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(net_worth_mod.render_csv_rows(timeline))

    n = len(timeline.points)
    latest = f", latest £{timeline.points[-1].net_worth_gbp:,.2f}" if n else ""
    err_console.print(
        f"Wrote net-worth report to {out_dir} ({n} point(s){latest})"
    )
    gap_n = len(timeline.missing_prices) + len(timeline.rate_gaps)
    if gap_n:
        err_console.print(
            f"[yellow]{gap_n} holding-snapshot(s) excluded (no mark / no "
            "GBP rate) — the timeline understates; see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command()
def allocation(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``allocation_reports_dir`` (``reports/allocation``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    property_source: PropertyOpt = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", "-s",
            help="Exit non-zero if any holding couldn't be valued (no mark / "
            "no GBP rate), so a point's allocation understates.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Asset allocation over time.

    Values each statement at its own date (reusing the concentration
    valuation) and tracks the asset-class mix — equity / bond / property /
    … plus net cash — across the combined timeline, writing
    ``allocation.md`` + ``allocation.csv``. Weights are a share of gross
    long holdings (cash / leverage shown separately). A reporting aid.
    """

    _configure_logging(verbose)
    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    timeline = allocation_mod.build_timeline(
        texts, commodities=commodities_map, rate_source=rates,
        properties=_load_properties(property_source),
    )

    out_dir = out or settings.allocation_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "allocation.md").write_text(
        allocation_mod.render_markdown(timeline), encoding="utf-8"
    )
    with (out_dir / "allocation.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(allocation_mod.render_csv_rows(timeline))

    err_console.print(
        f"Wrote allocation report to {out_dir} ({len(timeline.points)} point(s))"
    )
    gap_n = len(timeline.missing_prices) + len(timeline.rate_gaps)
    if gap_n:
        err_console.print(
            f"[yellow]{gap_n} holding(s) excluded (no mark / no GBP rate) "
            "— see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command("portfolio-allocation")
def portfolio_allocation(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``portfolio_allocation_reports_dir`` "
            "(``reports/portfolio-allocation``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    property_source: PropertyOpt = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", "-s",
            help="Exit non-zero if any holding couldn't be valued (no mark / "
            "no GBP rate).",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Per-portfolio allocation.

    Breaks the latest valuation down per portfolio (each Pictet account,
    the Vanguard ISA, each property), showing each portfolio's asset-class
    + holdings allocation and its share of total net worth. Writes
    ``portfolio-allocation.md`` + ``portfolio-allocation.csv``. A reporting
    aid.
    """

    _configure_logging(verbose)
    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    report = portfolio_allocation_mod.build_report(
        texts, commodities=commodities_map, rate_source=rates,
        properties=_load_properties(property_source),
    )

    out_dir = out or settings.portfolio_allocation_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "portfolio-allocation.md").write_text(
        portfolio_allocation_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "portfolio-allocation.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        csv.writer(fh).writerows(portfolio_allocation_mod.render_csv_rows(report))

    err_console.print(
        f"Wrote portfolio-allocation report to {out_dir} "
        f"({len(report.portfolios)} portfolio(s))"
    )
    gap_n = len(report.missing_prices) + len(report.rate_gaps)
    if gap_n:
        err_console.print(
            f"[yellow]{gap_n} holding(s) excluded (no mark / no GBP rate) "
            "— see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command()
def income(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked recursively for *.transactions.jsonl "
            "sidecars (the same substrate tax-report reads). Defaults to "
            "``data``.",
        ),
    ] = Path("data"),
    period: Annotated[
        str,
        typer.Option(
            "--period",
            help="Grouping period: ``tax-year`` (6 Apr–5 Apr, YYYY-YY) or "
            "``calendar`` (Jan–Dec). Defaults to ``tax-year``.",
        ),
    ] = "tax-year",
    commodities: Annotated[
        Path | None,
        typer.Option(
            "--commodities",
            help="Commodity-metadata TOML (drives the bond-fund "
            "distribution→interest reclassification + holding names). "
            "Defaults to the configured ``commodities_metadata_path``.",
        ),
    ] = None,
    rate_source: Annotated[
        str | None,
        typer.Option(
            "--rate-source",
            help="GBP rate source for non-GBP income "
            "(``null`` | ``hmrc-monthly``). Defaults to the configured source.",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``income_reports_dir`` (``reports/income``).",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", "-s",
            help="Exit non-zero if any income amount lacked a GBP rate "
            "(and so was excluded from the totals).",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Income by source (dividends + interest received).

    Reads the JSONL sidecars under ``--source``, aggregates dividend and
    interest income by period and paying source, converts to GBP, and
    writes ``income.md`` + ``income.csv``. Unlike the tax reports this
    *includes* ISA income (flagged tax-free), since it's genuine income.
    A reporting aid, not advice.
    """

    _configure_logging(verbose)
    if period not in ("tax-year", "calendar"):
        err_console.print(
            "[red]error:[/red] --period must be 'tax-year' or 'calendar'"
        )
        raise typer.Exit(code=2)

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

    # No ISA filter here (cf. tax-report): an ISA's income is real income,
    # just tax-free — the report flags the wrapper instead of dropping it.
    txns = _load_sidecar_transactions(source)
    report = income_mod.compute_income(
        txns, period=cast(income_mod.PeriodMode, period),
        commodities=commodities_map, source=rates,
    )

    out_dir = out or settings.income_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "income.md").write_text(
        income_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "income.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(income_mod.render_csv_rows(report))

    err_console.print(
        f"Wrote income report to {out_dir} ({len(report.rows)} source-row(s))"
    )
    if report.missing_rates:
        err_console.print(
            f"[yellow]{len(report.missing_rates)} income amount(s) excluded "
            "(no GBP rate) — see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command("trial-balance")
def trial_balance(
    ledger: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Beancount ledger to query. Defaults to ``main.beancount``.",
        ),
    ] = Path("main.beancount"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``trial_balance_reports_dir`` (``reports/trial-balance``).",
        ),
    ] = None,
    rate_source: ValuationRateSourceOpt = None,
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--as-of",
            formats=["%Y-%m-%d"],
            help="Date for the GBP rate lookup (the marks are the ledger's "
            "latest prices regardless). Defaults to today.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero if any Asset/Liability balance can't be "
            "valued in GBP (no mark or no rate).",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Per-account trial balance from the ledger, with a GBP column on Assets.

    Lists every account's closing balance via ``bean-query`` (securities in
    units, cash native); the Assets / Liabilities sections add a GBP
    market-value column (latest mark converted at the configured rate),
    while Equity / Income / Expenses stay native. Writes ``trial-balance.md``
    + ``trial-balance.csv``. Needs the ``bean-query`` binary (``uv tool
    install beancount``); a missing binary is a warning, not an error.
    """

    _configure_logging(verbose)
    eff_settings = (
        settings.model_copy(update={"gbp_rate_source": rate_source})
        if rate_source is not None
        else settings
    )
    rates = build_rate_source(eff_settings)
    on_date = as_of.date() if as_of is not None else date.today()

    result = trial_balance_mod.query_balances(ledger)
    if result.binary_missing:
        err_console.print(f"[yellow]warning:[/yellow] {result.error}")
        raise typer.Exit(code=0)
    if not result.ok:
        err_console.print(f"[red]bean-query failed[/red]:\n{result.error}")
        raise typer.Exit(code=1)

    tb = trial_balance_mod.build_trial_balance(
        result, on_date=on_date, rate_source=rates
    )

    out_dir = out or settings.trial_balance_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trial-balance.md").write_text(
        "\n".join(trial_balance_mod.render_markdown(tb)), encoding="utf-8"
    )
    with (out_dir / "trial-balance.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        csv.writer(fh).writerows(trial_balance_mod.render_csv_rows(tb))

    err_console.print(
        f"Wrote trial balance to {out_dir} ({len(tb.lines)} account(s); "
        f"assets £{tb.assets_gbp:,.2f} at market)"
    )
    gap_n = len(tb.missing_prices) + len(tb.rate_gaps)
    if gap_n:
        err_console.print(
            f"[yellow]{gap_n} Asset/Liability balance(s) not valued in GBP "
            "(no mark / no rate) — see the report.[/yellow]"
        )
        if strict:
            raise typer.Exit(code=1)


@app.command("balance-sheet")
def balance_sheet(
    ledger: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Beancount ledger to query. Defaults to ``main.beancount``.",
        ),
    ] = Path("main.beancount"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``balance_sheet_reports_dir`` (``reports/balance-sheet``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open",
            help="Open the generated HTML in the default browser.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Build the interactive balance-sheet HTML you can scrub to any date.

    One self-contained, offline ``balance-sheet.html`` (+ a
    ``balance-sheet-data.json`` sidecar): the whole book's Asset/Liability
    holdings queried once via ``bean-query``, valued client-side to GBP at
    whatever as-of date you pick. Needs the ``bean-query`` binary (``uv tool
    install beancount``); a missing binary is a warning, not an error. The
    output carries real balances, so its directory is git-ignored.
    """

    _configure_logging(verbose)
    eff_settings = (
        settings.model_copy(update={"gbp_rate_source": rate_source})
        if rate_source is not None
        else settings
    )
    rates = build_rate_source(eff_settings)
    cpath = commodities or settings.commodities_metadata_path
    commodities_map = (
        load_commodities(cpath) if cpath is not None and cpath.is_file() else {}
    )
    data_dir = ledger.parent / "data"

    data, result = balance_sheet_mod.build_data(
        ledger,
        commodities=commodities_map,
        rate_source=rates,
        prices_path=data_dir / "prices.beancount",
        assertions_path=data_dir / "balances.beancount",
    )
    if result.binary_missing:
        err_console.print(f"[yellow]warning:[/yellow] {result.error}")
        raise typer.Exit(code=0)
    if data is None:
        err_console.print(f"[red]bean-query failed[/red]:\n{result.error}")
        raise typer.Exit(code=1)

    out_dir = out or settings.balance_sheet_reports_dir
    html_path = balance_sheet_mod.write_artifact(data, out_dir)
    err_console.print(
        f"Wrote {html_path} ({len(data.postings)} posting(s), "
        f"as-of {data.as_of_min}…{data.as_of_max})"
    )
    if open_browser:
        import webbrowser

        webbrowser.open(html_path.resolve().as_uri())


@app.command("mandate-scorecard")
def mandate_scorecard(
    ledger: Annotated[
        Path,
        typer.Option(
            "--ledger",
            exists=True,
            readable=True,
            help="Ledger to read costs from (Expenses:Pic). Defaults to "
            "``main.beancount``.",
        ),
    ] = Path("main.beancount"),
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``mandate_scorecard_reports_dir`` (``reports/mandate-scorecard``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    property_source: PropertyOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Mandate cost scorecard (step 1) — the all-in explicit cost block.

    Totals the mandate's ledger-visible cost per calendar year — management
    fee, transaction & custody, and Lombard interest — from the
    ``Expenses:Pic`` accounts (excluding payment/transfer ``Other`` legs and
    the in-house fund TERs, which aren't in the ledger), converted to GBP,
    and expressed as a share of the year's average invested assets (gross
    long, from the net-worth timeline). Needs ``bean-query`` for the costs
    and statements for the average-assets denominator.
    """

    _configure_logging(verbose)
    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    timeline = net_worth_mod.build_timeline(
        texts, commodities=commodities_map, rate_source=rates,
        properties=_load_properties(property_source),
    )

    result = mandate_scorecard_mod.query_costs(ledger)
    if result.binary_missing:
        err_console.print(f"[yellow]warning:[/yellow] {result.error}")
        raise typer.Exit(code=0)
    if not result.ok:
        err_console.print(f"[red]bean-query failed[/red]:\n{result.error}")
        raise typer.Exit(code=1)

    report = mandate_scorecard_mod.build_cost_report(
        result, rate_source=rates, timeline=timeline
    )

    out_dir = out or settings.mandate_scorecard_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mandate-scorecard.md").write_text(
        mandate_scorecard_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "mandate-scorecard.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        csv.writer(fh).writerows(mandate_scorecard_mod.render_csv_rows(report))

    total = sum((c.total_gbp for c in report.years), __import__("decimal").Decimal(0))
    err_console.print(
        f"Wrote mandate scorecard to {out_dir} "
        f"({len(report.years)} year(s), total explicit cost £{total:,.2f})"
    )


@app.command("mandate-returns")
def mandate_returns(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked recursively for *.transactions.jsonl "
            "sidecars — supplies the distribution income the price-only "
            "holdings gain misses. Defaults to ``data``.",
        ),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``mandate_returns_reports_dir`` (``reports/mandate-returns``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Mandate returns (step 2) — time- & money-weighted returns.

    Computes the Pictet mandate's return on two bases side by side — **net**
    (your equity, assets minus the Lombard loan) and **gross** (the total
    asset book, loan added back) — as a time-weighted return (TWR, the
    manager's scorecard) and a money-weighted return (MWR/XIRR, your actual
    experience). Whole mandate plus a per-account (K / P) breakdown.

    Computed **from the statement holdings** (price moves on units held
    through each pair of statements), so it needs no flow tagging in the
    ledger — deposits and withdrawals never read as performance and emerge
    instead as a "detected movements" table. The sidecars under ``--source``
    supply the distribution income the price-only gain misses; no bean-query.
    """

    _configure_logging(verbose)
    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )

    report = mandate_returns_mod.build_report(
        texts, commodities=commodities_map, rate_source=rates,
        transactions=_load_sidecar_transactions(source),
    )

    out_dir = out or settings.mandate_returns_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mandate-returns.md").write_text(
        mandate_returns_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "mandate-returns.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        csv.writer(fh).writerows(mandate_returns_mod.render_csv_rows(report))

    agg = report.aggregate

    def _p(v: float | None) -> str:
        return "—" if v is None else f"{v * 100:.1f}%"

    err_console.print(
        f"Wrote mandate returns to {out_dir} "
        f"(TWR p.a. net {_p(agg.twr_net_annualised)} / gross "
        f"{_p(agg.twr_gross_annualised)}, MWR {_p(agg.mwr_net)})"
    )
    if report.detected_flows:
        err_console.print(
            f"[dim]{len(report.detected_flows)} external movement(s) inferred "
            "from the holdings — see the report.[/dim]"
        )


@app.command("benchmark")
def benchmark(
    statements: StatementOpt = [],  # noqa: B006 — list-option default lives here
    statements_dir: StatementsDirOpt = None,
    statements_recursive: StatementsRecursiveOpt = False,
    benchmarks: Annotated[
        Path | None,
        typer.Option(
            "--benchmarks",
            exists=True,
            readable=True,
            help="Benchmark index-levels CSV (date + one column per "
            "benchmark, GBP total-return levels). Defaults to the configured "
            "``benchmark_path`` (``data/benchmarks.csv``).",
        ),
    ] = None,
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory walked recursively for *.transactions.jsonl "
            "sidecars — supplies the distribution income folded into the "
            "mandate return. Defaults to ``data``.",
        ),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``benchmark_reports_dir`` (``reports/benchmark``).",
        ),
    ] = None,
    commodities: CommoditiesOpt = None,
    rate_source: ValuationRateSourceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Mandate value-add vs benchmarks (step 3).

    Compares the mandate's **gross** (unlevered) time-weighted return against
    each passive benchmark over the window it covers, so the difference
    isolates what active management added over holding the index. The
    benchmark CSV is index *levels* (GBP total-return), sampled at the
    mandate's statement dates. Not risk-adjusted; a reporting aid, not
    advice.
    """

    _configure_logging(verbose)
    bench_path = benchmarks or settings.benchmark_path
    if bench_path is None or not bench_path.is_file():
        err_console.print(
            "[red]No benchmark CSV — pass --benchmarks or set benchmark_path "
            "(run scripts/fetch_benchmarks.py to generate "
            "data/benchmarks.csv).[/red]"
        )
        raise typer.Exit(code=2)

    texts, commodities_map, rates = _load_statement_context(
        statements, statements_dir, statements_recursive, commodities, rate_source
    )
    periods = mandate_returns_mod.aggregate_period_returns(
        texts, commodities=commodities_map, rate_source=rates,
        transactions=_load_sidecar_transactions(source),
    )
    bench_series = mandate_benchmark_mod.load_benchmarks(bench_path)
    report = mandate_benchmark_mod.build_report(periods, bench_series)

    out_dir = out or settings.benchmark_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "benchmark-value-add.md").write_text(
        mandate_benchmark_mod.render_markdown(report), encoding="utf-8"
    )
    with (out_dir / "benchmark-value-add.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        csv.writer(fh).writerows(mandate_benchmark_mod.render_csv_rows(report))

    err_console.print(
        f"Wrote benchmark value-add to {out_dir} "
        f"({len(report.rows)} benchmark(s) compared)"
    )
    if report.skipped:
        err_console.print(
            f"[yellow]{len(report.skipped)} benchmark(s) skipped — no "
            "overlapping data: " + ", ".join(report.skipped) + "[/yellow]"
        )


def _discover_financial_statements(directory: Path) -> list[Path]:
    """Walk ``directory`` (recursive) for ``Financial-statement-*.pdf``.

    Case-insensitive on the suffix so ``.PDF`` siblings are picked up, like
    the prices/scan discovery. Returns a sorted, de-duplicated list.
    """

    pattern = "Financial-statement-*.pdf"
    seen: set[Path] = set()
    for pat in {pattern, pattern.lower(), pattern.upper()}:
        for candidate in directory.rglob(pat):
            if candidate.is_file():
                seen.add(candidate)
    return sorted(seen)


@app.command()
def completeness(
    statements: Annotated[
        list[Path],
        typer.Option(
            "--statement",
            "-S",
            help="A Financial-statement PDF (repeatable). Combine with "
            "--statements-dir to scan a tree.",
        ),
    ] = [],  # noqa: B006 — list-option default lives here
    statements_dir: Annotated[
        Path | None,
        typer.Option(
            "--statements-dir",
            help="Directory scanned recursively for "
            "``Financial-statement-*.pdf``.",
        ),
    ] = None,
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Directory holding the ``*.transactions.jsonl`` sidecars. "
            "Defaults to ``data``.",
        ),
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output directory. Defaults to the configured "
            "``completeness_dir`` (``reports/completeness``).",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            "-s",
            help="Also exit non-zero on UNMATCHED-in-ledger events (an "
            "ingested cash event with no statement line). MISSING-in-ledger "
            "always fails regardless of this flag.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Cross-check the statement cash ledger against the ingested sidecars.

    The Pictet current-account statement is the authoritative list of every
    cash movement for its period. This diffs that list against the
    ``*.transactions.jsonl`` sidecars and writes one
    ``summary-<portfolio>-<period-end>.txt`` +
    ``findings-<portfolio>-<period-end>.csv`` per statement (keyed so
    successive runs / multiple portfolios don't clobber): statement
    lines with no ingested advice (MISSING-in-ledger — a likely un-ingested
    document), and ingested cash events with no statement line
    (UNMATCHED-in-ledger — a possible misdated booking). Securities
    settlements and out-of-period events are excluded, not flagged. Exits
    non-zero on any MISSING; with ``--strict`` also on any UNMATCHED.
    """

    _configure_logging(verbose)

    paths = list(statements)
    if statements_dir is not None:
        discovered = _discover_financial_statements(statements_dir)
        paths += discovered
        err_console.print(
            f"[dim]Discovered {len(discovered)} statement(s) under "
            f"{statements_dir}[/dim]"
        )
    if not paths:
        err_console.print(
            "[red]No statements given — pass --statement or "
            "--statements-dir.[/red]"
        )
        raise typer.Exit(code=2)

    out_dir = out or settings.completeness_dir
    total_missing, total_unmatched, written = _run_completeness(
        paths, source, out_dir
    )

    if written == 0:
        err_console.print(
            "[yellow]No parseable current-account statements.[/yellow]"
        )
        raise typer.Exit(code=0)

    err_console.print(
        f"Wrote {written} completeness report(s) to {out_dir} "
        f"({total_missing} missing, {total_unmatched} unmatched)"
    )
    if total_missing or (strict and total_unmatched):
        raise typer.Exit(code=1)
