"""Statement / ledger aggregation commands.

``prices`` and ``balances`` (extract price / balance-assertion directives
from per-trade output + statement valuations), ``portfolio`` (the central
account-opens aggregate), and ``property`` (generate the residential-property
ledger). Shared statement-discovery / commodity / property helpers come
from :mod:`banking_pipeline.cli._main`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from banking_pipeline import (
    balances_extract,
    portfolio_aggregate,
    prices_extract,
)
from banking_pipeline.cli._main import (
    _classify_paths,
    _configure_logging,
    _discover_priced_statements,
    _resolve_commodities,
    _resolve_name_to_isin,
    app,
    console,
    err_console,
)
from banking_pipeline.cli_options import (
    VerboseOpt,
)
from banking_pipeline.config import settings
from banking_pipeline.fx.gbp_rates import build_rate_source
from banking_pipeline.models import DocumentType
from banking_pipeline.property import load_properties, render_beancount


@app.command()
def prices(
    data_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Directory containing per-year *.beancount ingest output.",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Prices output file. Defaults to "
            "``<data_dir>/prices.beancount``.",
        ),
    ] = None,
    statements: Annotated[
        list[Path],
        typer.Option(
            "--statement",
            help="Pictet monthly-statement PDF (or pre-extracted "
            "``.txt`` dump). Repeat the flag for multiple statements. "
            "The Portfolio valuation page's per-ISIN market prices "
            "are merged into the trade-derived price database; on "
            "(date, ISIN) collisions the statement value wins because "
            "it's the authoritative quote for that date.",
        ),
    ] = [],  # noqa: B006 — Typer's documented list-option default; not mutated
    statements_dir: Annotated[
        Path | None,
        typer.Option(
            "--statements-dir",
            help="Directory to scan for Pictet monthly statements. Each "
            "PDF found is run through the layered classifier; only "
            "documents classified as a monthly statement (EN "
            "``MONTHLY_STATEMENT`` or ES ``ESTADO_MENSUAL``) are fed "
            "into the price extractor — quarterly and annual reports "
            "are skipped because they don't carry the per-ISIN "
            "Portfolio valuation page. Combine with explicit "
            "``--statement`` flags freely; merging is last-wins on "
            "(date, ISIN).",
        ),
    ] = None,
    statements_recursive: Annotated[
        bool,
        typer.Option(
            "--statements-recursive",
            "-R",
            help="Descend into subdirectories under ``--statements-dir``. "
            "Off by default so an accidental ``--statements-dir ~`` "
            "doesn't spider the home folder.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Extract ``price`` directives and write a beancount
    price-database file under ``data_dir``.

    Two complementary sources:

      - **Per-trade inventory annotations** (always on): the
        ``{<price> <ccy>}`` cost-basis braces on buys and the
        ``{} @ <price> <ccy>`` market-price annotation on sells in
        ``data_dir/*.beancount`` give one price point per trade
        date per ISIN.
      - **Monthly-statement valuations** (opt-in via ``--statement``
        or ``--statements-dir``): Pictet's portfolio-valuation page
        lists every held ISIN's market price on the statement date,
        so a year of monthly statements gives ~12 price points per
        holding regardless of whether the position traded that
        month. This is what densifies the price timeline for stale
        holdings (a fund bought in 2022 and held since trades-derives
        only one price on the buy date; statements add monthly
        quotes from then on).

    ``--statements-dir`` is the bulk equivalent of repeated
    ``--statement`` flags: it walks a directory, classifies every
    PDF, and keeps the ones whose document type carries a Portfolio
    valuation page. Add ``--statements-recursive`` to descend into
    subdirectories.
    """

    _configure_logging(verbose)

    discovered: dict[Path, DocumentType] = {}
    if statements_dir is not None:
        discovered = _discover_priced_statements(
            statements_dir, recursive=statements_recursive
        )
        err_console.print(
            f"[dim]Discovered {len(discovered)} monthly statement(s) "
            f"under {statements_dir}"
            + (" (recursive)" if statements_recursive else "")
            + "[/dim]"
        )

    # Explicit ``--statement`` paths come without classifications
    # attached; classify them here so non-monthly statements get the
    # same skip-with-info-log treatment as the directory-scan path
    # (point-6 tightening). Discovery results are pre-classified so
    # they don't need a second pass.
    explicit_doctypes = _classify_paths(statements) if statements else {}
    statement_doctypes: dict[Path, DocumentType] = {
        **explicit_doctypes,
        **discovered,
    }
    all_statements = list(statements) + list(discovered)
    output_path, total = prices_extract.generate(
        data_dir=data_dir,
        output=output,
        statement_files=all_statements,
        statement_doctypes=statement_doctypes,
    )
    extras = (
        f", {len(all_statements)} statement(s) merged" if all_statements else ""
    )
    err_console.print(
        f"Wrote {output_path} ({total} price directive(s){extras})"
    )


@app.command()
def property(  # noqa: A001 — command name, not the builtin
    source: Annotated[
        Path | None,
        typer.Option(
            "--source",
            help="Property TOML. Defaults to the configured "
            "``property_path`` (``data/property.toml``).",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o",
            help="Generated ledger file. Defaults to the configured "
            "``property_ledger_path`` (``data/property.beancount``).",
        ),
    ] = None,
    rate_source: Annotated[
        str | None,
        typer.Option(
            "--rate-source",
            help="GBP rate source for the GBP price mark on non-GBP "
            "properties (``null`` | ``hmrc-monthly``). Defaults to the "
            "configured source.",
        ),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Generate the residential-property ledger from ``data/property.toml``.

    Each property becomes a commodity held at cost (1 unit) and revalued by
    ``price`` directives, funded against ``Equity:Property:<label>`` (the
    financing already sits on the investment ledger). ``include`` the output
    from ``main.beancount`` to bring property onto the bean-check / Fava
    ledger.
    """

    _configure_logging(verbose)
    spath = source or settings.property_path
    if spath is None or not spath.is_file():
        err_console.print(
            "[red]No property TOML found — pass --source or create "
            "data/property.toml (see data/property.example.toml).[/red]"
        )
        raise typer.Exit(code=2)

    properties = load_properties(spath)
    eff_settings = (
        settings.model_copy(update={"gbp_rate_source": rate_source})
        if rate_source is not None
        else settings
    )
    rates = build_rate_source(eff_settings)

    out_path = output or settings.property_ledger_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_beancount(properties, rate_source=rates), encoding="utf-8"
    )
    err_console.print(
        f"Wrote {out_path} ({len(properties)} property/properties). "
        "Add `include \"" + str(out_path) + "\"` to main.beancount."
    )


@app.command()
def balances(
    data_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Directory where ``balances.beancount`` will be written.",
        ),
    ],
    statements: Annotated[
        list[Path],
        typer.Option(
            "--statement",
            help="Pictet monthly-statement PDF (or pre-extracted "
            "``.txt`` dump). Repeat for multiple statements; one "
            "balance assertion per holding and per cash sub-account "
            "is emitted, dated one day after each statement's "
            "``As at`` anchor (beancount's beginning-of-day "
            "evaluation convention).",
        ),
    ] = [],  # noqa: B006 — Typer's documented list-option default; not mutated
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Balances output file. Defaults to "
            "``<data_dir>/balances.beancount``.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Reconcile each statement against itself: re-scan for "
            "holdings / cash rows the parser dropped (a row visible in the "
            "statement but absent from the extraction silently understates "
            "the valuation). Reports any gap and exits non-zero.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Extract per-holding and per-cash-sub-account balance
    assertions from Pictet monthly statements.

    The output is consumed by ``bean-check`` on every load: the
    moment the running ledger drifts from the statement's recorded
    inventory (a missed ingest, an extraction bug, a writer
    regression), the next load fails with the source statement
    date in the error message.
    """

    _configure_logging(verbose)
    name_to_isin = _resolve_name_to_isin()
    output_path, total = balances_extract.generate(
        data_dir=data_dir,
        statement_files=statements,
        output=output,
        name_to_isin=name_to_isin,
    )
    err_console.print(
        f"Wrote {output_path} ({total} balance assertion(s) "
        f"from {len(statements)} statement(s))"
    )

    if strict:
        gaps = balances_extract.coverage_report(statements, name_to_isin)
        if gaps:
            for path, file_gaps in gaps:
                for gap in file_gaps:
                    err_console.print(
                        f"[red]coverage gap[/red] {path.name}: {gap.message}"
                    )
            raise typer.Exit(code=1)
        err_console.print("Coverage check passed: every statement reconciles.")


@app.command()
def portfolio(
    data_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Directory containing per-year *.beancount ingest output.",
        ),
    ],
    list_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--list-missing-metadata",
            help="Print one ISIN per line for in-use commodities that "
            "lack an entry in the commodity-metadata file, then exit "
            "without regenerating. Use it to keep "
            "``data/commodities.toml`` in sync with what's traded.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Aggregate output file. Defaults to "
            "``<data_dir>/portfolio.beancount``.",
        ),
    ] = None,
    operating_currency: Annotated[
        list[str],
        typer.Option(
            "--operating-currency",
            help="Currency emitted as ``option \"operating_currency\" \"<ccy>\"`` "
            "at the top of the aggregate. Pass multiple times for a "
            "multi-currency view; defaults to ``GBP``.",
        ),
    ] = ["GBP"],  # noqa: B006 — Typer's documented list-option default; not mutated
    booking_method: Annotated[
        str,
        typer.Option(
            "--booking-method",
            help="Inventory-reduction policy on sells. One of "
            "``FIFO`` (first-in-first-out, the default), ``LIFO``, "
            "``AVERAGE`` (weighted-average; deprecated in beancount v3), "
            "``STRICT`` (sells must specify lot labels), or ``NONE``. "
            "Pass an empty string to omit the directive.",
        ),
    ] = "FIFO",
    verbose: VerboseOpt = False,
) -> None:
    """Regenerate the portfolio-aggregate file (central account opens
    + per-year includes) under ``data_dir``."""

    _configure_logging(verbose)
    commodities = _resolve_commodities()

    if list_missing_metadata:
        known = set(commodities or {})
        missing = sorted(portfolio_aggregate.discover_isins(data_dir) - known)
        for isin in missing:
            console.print(isin)
        raise typer.Exit()

    output_path, total = portfolio_aggregate.generate(
        data_dir=data_dir,
        output=output,
        operating_currencies=operating_currency,
        booking_method=booking_method or None,
        commodities=commodities,
        ignore=(settings.property_ledger_path.name,),
    )
    err_console.print(
        f"Wrote {output_path} ({total} accounts; "
        f"operating_currency={','.join(operating_currency)})"
    )


@app.command("portfolio-split")
def portfolio_split(
    data_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Directory containing per-year *.beancount ingest output.",
        ),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory for the per-account ledgers. Defaults to "
            "``<data_dir>/accounts``.",
        ),
    ] = None,
    operating_currency: Annotated[
        list[str],
        typer.Option(
            "--operating-currency",
            help="Currency emitted as ``option \"operating_currency\" \"<ccy>\"`` "
            "at the top of each per-account ledger. Pass multiple times for "
            "a multi-currency view; defaults to ``GBP``.",
        ),
    ] = ["GBP"],  # noqa: B006 — Typer's documented list-option default; not mutated
    booking_method: Annotated[
        str,
        typer.Option(
            "--booking-method",
            help="Inventory-reduction policy on sells (``FIFO`` default, "
            "``LIFO`` / ``AVERAGE`` / ``STRICT`` / ``NONE``). Pass an empty "
            "string to omit the directive.",
        ),
    ] = "FIFO",
    root_ledger: Annotated[
        Path,
        typer.Option(
            "--root-ledger",
            help="Ledger to copy ``inferred_tolerance_default`` options "
            "from, so each per-account file balances standalone under the "
            "same rounding tolerances. Defaults to ``main.beancount``; "
            "missing-file or no-tolerance is a no-op.",
        ),
    ] = Path("main.beancount"),
    verbose: VerboseOpt = False,
) -> None:
    """Write one independently-loadable ledger per bank account.

    Groups the per-year ingest output by owning account (each Pictet
    account, the Vanguard ISA) and writes ``<account>.beancount`` for each
    under ``--output-dir`` — its own options, opens, closes, and includes
    of that account's per-year files plus ``prices.beancount``. Intended
    for opening a single account in isolation in Fava. ``balances.beancount``
    is not included (its assertions span every account, so an isolated
    ledger would fail bean-check on accounts it doesn't open).

    Per-currency rounding tolerances are copied from ``--root-ledger``
    (``main.beancount``) so each file balances on its own."""

    _configure_logging(verbose)
    commodities = _resolve_commodities()

    written = portfolio_aggregate.generate_per_account(
        data_dir=data_dir,
        output_dir=output_dir,
        operating_currencies=operating_currency,
        booking_method=booking_method or None,
        commodities=commodities,
        ignore=(settings.property_ledger_path.name,),
        extra_options=portfolio_aggregate.inferred_tolerance_options(root_ledger),
    )
    if not written:
        err_console.print(
            "[yellow]warning:[/yellow] no bank accounts found in "
            f"{data_dir} — nothing written"
        )
        return
    for path, account_key, total in written:
        err_console.print(f"Wrote {path} ({account_key}, {total} accounts)")
