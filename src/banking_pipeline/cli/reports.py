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
from banking_pipeline import concentration as concentration_mod
from banking_pipeline import income as income_mod
from banking_pipeline import net_worth as net_worth_mod
from banking_pipeline import portfolio_allocation as portfolio_allocation_mod
from banking_pipeline import trial_balance as trial_balance_mod
from banking_pipeline.cli._main import (
    _configure_logging,
    _load_properties,
    _load_sidecar_transactions,
    _load_statement_context,
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
        properties=_load_properties(property_source),
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
