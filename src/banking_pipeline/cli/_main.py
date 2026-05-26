"""Typer CLI entrypoint."""

from __future__ import annotations

import csv
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, cast

import structlog
import typer
from rich.console import Console

from banking_pipeline import (
    allocation as allocation_mod,
)
from banking_pipeline import (
    balances_extract,
    bean_check,
    beancount_writer,
    portfolio_aggregate,
    prices_extract,
)
from banking_pipeline import (
    concentration as concentration_mod,
)
from banking_pipeline import (
    income as income_mod,
)
from banking_pipeline import (
    net_worth as net_worth_mod,
)
from banking_pipeline import (
    portfolio_allocation as portfolio_allocation_mod,
)
from banking_pipeline import (
    reconcile as reconcile_mod,
)
from banking_pipeline.batch_config import (
    BatchConfig,
    ReportsStep,
    Source,
    load_config,
)
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.cli_options import (
    VerboseOpt,
)
from banking_pipeline.commodities_metadata import CommodityMetadata, load_commodities
from banking_pipeline.config import settings
from banking_pipeline.extractors import load_pdf
from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.fx.gbp_rates import GbpRateSource, build_rate_source
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.pipeline import Pipeline
from banking_pipeline.property import Property, load_properties, render_beancount
from banking_pipeline.transaction_sidecar import (
    dump_transactions,
    load_transactions,
    sidecar_path,
)

app = typer.Typer(help="Ingest banking PDFs and emit beancount entries.")
# ``soft_wrap=True`` stops rich from hard-wrapping at the detected
# terminal width (80 cols when output is piped/captured). That matters
# on two fronts: diagnostic lines carry long absolute paths, and
# ``console`` prints rendered beancount whose account paths exceed 80
# cols — a hard wrap there would corrupt the emitted ledger.
console = Console(soft_wrap=True)
err_console = Console(stderr=True, soft_wrap=True)


def _configure_logging(verbose: bool, *, quiet: bool = False) -> None:
    # Logs go to stderr so stdout stays a clean data stream (rendered
    # beancount, ``dump-transactions`` JSONL) that can be piped. ``quiet``
    # raises the threshold above every level so commands whose stdout is
    # machine-readable don't interleave INFO chatter when not --verbose.
    # quiet (and not --verbose) → CRITICAL, above every level the
    # pipeline emits; otherwise DEBUG with --verbose, else INFO.
    level = 50 if (quiet and not verbose) else (10 if verbose else 20)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


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


def _statement_text(path: Path) -> str:
    """Read a statement path to text — a ``.txt`` dump verbatim, else the
    PDF extractor (deferred import, as elsewhere, so the txt path stays
    pypdfium2-free)."""

    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8")
    return load_pdf(path).text


def _load_statement_context(
    statements: list[Path],
    statements_dir: Path | None,
    statements_recursive: bool,
    commodities: Path | None,
    rate_source: str | None,
) -> tuple[list[tuple[str, str]], dict[str, CommodityMetadata], GbpRateSource]:
    """Resolve the inputs shared by the statement-valuation reports
    (``concentration`` / ``net-worth``): discover + load statement texts,
    the commodity metadata, and the GBP rate source. Exits (code 2) when no
    statements are given."""

    paths = list(statements)
    if statements_dir is not None:
        discovered = _discover_priced_statements(
            statements_dir, recursive=statements_recursive
        )
        paths += list(discovered)
        err_console.print(
            f"[dim]Discovered {len(discovered)} statement(s) under "
            f"{statements_dir}[/dim]"
        )
    if not paths:
        err_console.print(
            "[red]No statements given — pass --statement or --statements-dir.[/red]"
        )
        raise typer.Exit(code=2)

    texts = [(_statement_text(p), p.name) for p in paths]
    cpath = commodities or settings.commodities_metadata_path
    commodities_map = (
        load_commodities(cpath) if cpath is not None and cpath.is_file() else {}
    )
    eff_settings = (
        settings.model_copy(update={"gbp_rate_source": rate_source})
        if rate_source is not None
        else settings
    )
    return texts, commodities_map, build_rate_source(eff_settings)


def _load_properties(override: Path | None) -> list[Property]:
    """Off-ledger residential property for the valuation reports — the
    override path, else the configured ``property_path``, else none."""

    path = override or settings.property_path
    return load_properties(path) if path is not None and path.is_file() else []


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


def _discover_priced_statements(
    directory: Path, *, recursive: bool, pattern: str = "*.pdf"
) -> dict[Path, DocumentType]:
    """Walk ``directory`` for PDFs and keep those classified as
    monthly statements (the only doctype with per-ISIN pricing).

    Returns an insertion-ordered ``{path: DocumentType}`` mapping —
    callers downstream forward it to
    :func:`prices_extract.generate` as ``statement_doctypes`` so the
    parser can short-circuit non-monthly types loudly rather than
    silently producing empty rows.

    Mirrors the ``scan`` command's case-insensitive glob plumbing so
    ``*.pdf`` picks up ``.PDF`` and ``.Pdf`` siblings without making
    the caller OR-glob by hand. Files that fail to load or classify
    are skipped silently — this is a best-effort discovery walk, not
    an audit; a corrupt PDF in the tree shouldn't abort the prices
    rebuild.
    """

    walk = directory.rglob if recursive else directory.glob
    seen_paths: set[Path] = set()
    for pat in {pattern, pattern.lower(), pattern.upper()}:
        for candidate in walk(pat):
            if candidate.is_file():
                seen_paths.add(candidate)
    return _filter_priced_statements(sorted(seen_paths))


def _filter_priced_statements(
    paths: Iterable[Path],
) -> dict[Path, DocumentType]:
    """Return the subset of ``paths`` whose classification matches a
    pricing-bearing doctype (see
    :data:`prices_extract.PRICED_STATEMENT_DOCTYPES`), as a
    ``{path: DocumentType}`` mapping preserving input order.

    Files that fail to load or classify are silently dropped — see
    :func:`_discover_priced_statements` for the rationale.
    """

    classifier = LayeredClassifier()
    matches: dict[Path, DocumentType] = {}
    for path in paths:
        try:
            doc = load_pdf(path)
            classification = classifier.classify(doc)
        except Exception:  # noqa: BLE001 — best-effort filter
            continue
        if classification.document_type in prices_extract.PRICED_STATEMENT_DOCTYPES:
            matches[path] = classification.document_type
    return matches


def _classify_paths(paths: Iterable[Path]) -> dict[Path, DocumentType]:
    """Classify ``paths`` and return the full ``{path: DocumentType}``
    mapping — *no* doctype filtering applied.

    Used on the ``prices`` command's explicit ``--statement`` flag
    where the user specifically named the file: we still want the
    classification (so the parser can short-circuit non-monthly
    types loudly), but we don't drop the entry from the mapping
    because the user asked for it explicitly.
    """

    classifier = LayeredClassifier()
    out: dict[Path, DocumentType] = {}
    for path in paths:
        try:
            doc = load_pdf(path)
            classification = classifier.classify(doc)
        except Exception:  # noqa: BLE001 — best-effort
            continue
        out[path] = classification.document_type
    return out


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
    output_path, total = balances_extract.generate(
        data_dir=data_dir,
        statement_files=statements,
        output=output,
    )
    err_console.print(
        f"Wrote {output_path} ({total} balance assertion(s) "
        f"from {len(statements)} statement(s))"
    )


def _resolve_commodities() -> dict[str, CommodityMetadata] | None:
    """Load the configured commodity-metadata file, or ``None`` if unset.

    Returns ``None`` when no metadata path is configured / present, which
    tells :func:`portfolio_aggregate.generate` to skip the commodity
    block entirely (keeping the aggregate byte-identical to before the
    UK-tax work). An empty-but-present file loads as ``{}`` — every
    in-use ISIN then renders a stub.
    """

    path = settings.commodities_metadata_path
    if path is None or not path.is_file():
        return None
    return load_commodities(path)


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


@app.command()
def check(
    ledger: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Beancount ledger to validate. Can be the rebuild's "
            "``portfolio.beancount`` aggregate, a parent ``main.beancount`` "
            "that includes it, or any individual ``.beancount`` file.",
        ),
    ],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            "-s",
            help="Treat bean-check warnings as errors. Off by default — "
            "beancount emits warnings on benign conditions (missing "
            "prices for stale holdings etc.) that would otherwise noise "
            "up the output. Turn on for a strict CI gate.",
        ),
    ] = False,
) -> None:
    """Run ``bean-check`` against a ledger.

    Exits with the same return code as ``bean-check`` itself so cron /
    CI jobs can branch on success vs. failure. A missing ``bean-check``
    binary surfaces as a warning rather than a hard error — install
    with ``uv tool install beancount``. (We shell out rather than link
    against beancount because beancount is GPL-2.0; the README has
    the full licence rationale.)
    """

    _run_check_or_exit(ledger, strict=strict)


@app.command()
def reconcile(
    ledger: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Beancount ledger to reconcile — typically the "
            "``main.beancount`` root that includes the generated "
            "aggregate and the balance assertions.",
        ),
    ] = Path("main.beancount"),
    balances: Annotated[
        Path,
        typer.Option(
            "--balances",
            "-b",
            exists=True,
            readable=True,
            help="Statement-asserted balances file (the expected side). "
            "Defaults to ``data/balances.beancount``.",
        ),
    ] = Path("data/balances.beancount"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Directory for ``summary.txt`` / ``drift.csv``. Defaults "
            "to ``reconciliation_dir`` (``reports/reconciliation``).",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            "-s",
            help="Escalate coverage gaps (statement months with no "
            "assertion) to a nonzero exit. Drift always fails regardless "
            "of this flag.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Reconcile statement-asserted balances against the ledger.

    Runs ``bean-check`` over the ledger, parses its balance-assertion
    failures, and writes a full report: every drifted ``balance``
    directive with its signed difference, the earliest date each account
    diverged, and coverage gaps (statement months with no assertion).
    Additive to ``bean-check`` — it reports the whole grid and localises
    each divergence instead of just listing raw failures, and surfaces
    missing statement months a checkpoint can't.

    The drift verdict is ``bean-check``'s own, so reconcile agrees with a
    load by construction (beancount's inferred-from-decimals tolerance is
    honoured without re-implementing it). Exits nonzero on any drift;
    with ``--strict`` coverage gaps fail too.
    """

    _configure_logging(verbose)

    out_dir = output or settings.reconciliation_dir
    report = _run_reconcile(ledger, balances, out_dir)
    if report is None:
        # Nothing to reconcile (no assertions / no bean-check binary);
        # _run_reconcile already explained why. Not an error.
        raise typer.Exit(code=0)
    if report.has_drift or (strict and report.coverage_gaps):
        raise typer.Exit(code=1)


def _run_reconcile(
    ledger: Path, balances: Path, out_dir: Path
) -> reconcile_mod.ReconReport | None:
    """Reconcile statement balances against ``ledger`` and write the report.

    Shared by the ``reconcile`` command and the ``rebuild`` post-step so
    both behave identically. Runs ``bean-check`` once, parses its
    balance-assertion failures (matched back to the asserted line),
    builds the report, writes ``summary.txt`` / ``drift.csv`` under
    ``out_dir``, and echoes the summary.

    Returns the report, or ``None`` when reconciliation couldn't run —
    a missing balances file, no assertions in it, or no ``bean-check``
    binary. The caller treats ``None`` as "nothing to gate on". A
    missing binary is a warning (the user opted out of validation),
    consistent with the ``check`` step.
    """

    if not balances.exists():
        err_console.print(
            f"[yellow]reconcile skipped:[/yellow] balances file "
            f"{balances} doesn't exist"
        )
        return None
    assertions = reconcile_mod.parse_assertions(
        balances.read_text(encoding="utf-8")
    )
    if not assertions:
        err_console.print(
            f"[yellow]No balance assertions found in {balances}.[/yellow] "
            "Run `banking-pipeline balances` to generate them first."
        )
        return None

    # bean-check evaluates every assertion in one pass and prints a
    # failure line per drift. We parse its output regardless of return
    # code (it exits nonzero on drift, which is exactly the case we want
    # to report); only a missing binary stops us.
    result = bean_check.run_bean_check(ledger)
    if result.binary_missing:
        err_console.print(f"[yellow]warning:[/yellow] {result.stderr}")
        return None

    failures = reconcile_mod.parse_bean_check_failures(
        result.stderr, balances_name=balances.name
    )
    report = reconcile_mod.build_report(assertions, failures)

    out_dir.mkdir(parents=True, exist_ok=True)
    summary = reconcile_mod.render_summary(
        report, ledger=str(ledger), balances=str(balances)
    )
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    (out_dir / "drift.csv").write_text(
        reconcile_mod.render_csv(report), encoding="utf-8"
    )

    err_console.print(summary, markup=False, highlight=False, soft_wrap=True)
    err_console.print(f"Wrote {out_dir}/summary.txt and {out_dir}/drift.csv")
    return report


@app.command()
def rebuild(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to the rebuild config TOML. Defaults to "
            "``banking-pipeline.toml`` in the project root.",
        ),
    ] = None,
    project_root: Annotated[
        Path | None,
        typer.Option(
            "--project-root",
            help="Project root used to resolve relative paths and "
            "locate the default config. Defaults to the current "
            "working directory.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print what each step would do (delete / ingest / "
            "prices / portfolio / balances) without executing anything.",
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            "-s",
            help="Fail on quality issues instead of warning. Two effects: "
            "(a) when a per-template extractor returns zero transactions "
            "for a doctype that should produce output, raise instead of "
            "silently skipping the document; (b) the bean-check post-step "
            "treats warnings as errors regardless of the [post.check] "
            "config setting.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """End-to-end rebuild driven by ``banking-pipeline.toml``.

    Replaces the historical ``run.sh`` shell script with a typed,
    config-driven equivalent. The config lives in
    ``banking-pipeline.toml`` (gitignored — copy from
    ``banking-pipeline.example.toml`` and edit for your local folder
    layout). The command:

    1. Deletes stale outputs under ``<data_dir>/<clean_glob>``.
    2. Runs ``ingest`` once per ``[[sources]]`` entry, writing to
       ``<data_dir>/<label>.beancount``.
    3. Runs ``prices`` / ``portfolio`` / ``balances`` / ``reports`` /
       ``reconcile`` / ``check`` according to the ``[post]`` toggles.

    Globs that match zero files surface as a non-fatal warning rather
    than an error — handy when a year-partition hasn't received any
    documents yet. ``--dry-run`` previews every step without touching
    the filesystem; useful before the first real run on a new config.
    """

    _configure_logging(verbose)
    # Resolve the default here rather than in the signature: ``Path.cwd()``
    # as a parameter default is evaluated once at import, not per invocation.
    project_root = project_root or Path.cwd()
    cfg = load_config(project_root, config_path=config)
    _do_rebuild(cfg, project_root=project_root, dry_run=dry_run, strict=strict)


def _do_rebuild(
    cfg: BatchConfig,
    *,
    project_root: Path,
    dry_run: bool,
    strict: bool = False,
) -> None:
    """Execute (or preview) the steps described by ``cfg``."""

    data_dir = cfg.resolve_data_dir(project_root)
    if not dry_run:
        data_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 1: clean ----------------------------------------------------
    stale = list(cfg.stale_files(project_root))
    if stale:
        for path in stale:
            err_console.print(
                f"[dim]rm[/dim] {path}" if dry_run else f"Removing {path}"
            )
            if not dry_run:
                path.unlink()

    # --- Step 2: ingest ---------------------------------------------------
    pipeline: Pipeline | None = None
    for src in cfg.sources:
        out_path = data_dir / f"{src.label}.beancount"
        pdfs = src.expand(project_root)
        if not pdfs:
            err_console.print(
                f"[yellow]warning:[/yellow] source {src.label!r} matched "
                f"zero files (glob={src.glob!r}); skipping"
            )
            continue
        err_console.print(
            f"[bold]ingest[/bold] {src.label} → {out_path} "
            f"({len(pdfs)} PDF{'s' if len(pdfs) != 1 else ''})"
        )
        if dry_run:
            continue
        # Lazy-init the pipeline on the first ingest that actually runs —
        # keeps the dry-run path free of any heavy imports. ``strict``
        # propagates into HybridExtractor so a template returning [] for
        # a doctype that should produce output raises rather than warns.
        if pipeline is None:
            pipeline = Pipeline(extractor=HybridExtractor(strict=strict))
        chunks: list[str] = []
        src_txns: list[Transaction] = []
        for pdf in pdfs:
            try:
                result = pipeline.process(pdf)
            except TemplateExtractionError as exc:
                err_console.print(
                    f"[red]extraction error[/red] in source "
                    f"{src.label!r}: {exc}"
                )
                raise typer.Exit(code=1) from exc
            chunks.append(beancount_writer.render(result))
            src_txns.extend(result.transactions)
        out_path.write_text("\n\n".join(chunks), encoding="utf-8")
        # Structured sidecar next to the per-label ledger (header-only
        # when the source produced no transactions).
        dump_transactions(src_txns, sidecar_path(out_path))

    # --- Step 3: post-processing -----------------------------------------
    if cfg.post.prices:
        # Expand and pre-filter statement PDFs *before* the dry-run /
        # real-run branch so the dry-run preview shows how many
        # documents the prices step would actually consume. ``soft_wrap``
        # is on for both prints because the data_dir + prices.beancount
        # paths in this step regularly exceed Rich's default 80-col
        # width and the default cropping behaviour can swallow useful
        # diagnostic content (the ``M of N matched`` cell, the leading
        # ``wrote `` prefix) — soft-wrap keeps the whole line intact.
        price_doctypes: dict[Path, DocumentType] = {}
        if cfg.post.price_statements:
            expanded = _expand_globs(cfg.post.price_statements, project_root)
            price_doctypes = _filter_priced_statements(expanded)
            err_console.print(
                f"[bold]prices[/bold] {data_dir} "
                f"({len(price_doctypes)} of {len(expanded)} matched "
                f"statement(s) classified as monthly)",
                soft_wrap=True,
            )
        else:
            err_console.print(
                f"[bold]prices[/bold] {data_dir}", soft_wrap=True
            )
        if not dry_run:
            output_path, total = prices_extract.generate(
                data_dir=data_dir,
                output=None,
                statement_files=list(price_doctypes),
                statement_doctypes=price_doctypes,
            )
            extras = (
                f"; {len(price_doctypes)} statement(s) merged"
                if price_doctypes
                else ""
            )
            err_console.print(
                f"  wrote {output_path} ({total} price directive(s){extras})",
                soft_wrap=True,
            )

    if cfg.post.portfolio:
        operating = cfg.post.operating_currencies or ["GBP"]
        err_console.print(
            f"[bold]portfolio[/bold] {data_dir} "
            f"(operating={','.join(operating)})"
        )
        if not dry_run:
            output_path, total = portfolio_aggregate.generate(
                data_dir=data_dir,
                output=None,
                operating_currencies=operating,
                booking_method=cfg.post.booking_method or None,
                commodities=_resolve_commodities(),
                ignore=(settings.property_ledger_path.name,),
            )
            err_console.print(
                f"  wrote {output_path} ({total} accounts)"
            )

    if cfg.post.balances:
        statements = _expand_globs(cfg.post.balance_statements, project_root)
        err_console.print(
            f"[bold]balances[/bold] {data_dir} "
            f"({len(statements)} statement{'s' if len(statements) != 1 else ''})"
        )
        if not dry_run:
            output_path, total = balances_extract.generate(
                data_dir=data_dir,
                statement_files=statements,
                output=None,
            )
            err_console.print(
                f"  wrote {output_path} ({total} balance assertion(s))"
            )

    # --- Step 3.5: reports -----------------------------------------------
    # Read-only analytical reports. Runs before reconcile/check so the
    # Markdown/CSV always regenerate even when bean-check later exits
    # nonzero on drift.
    if cfg.post.reports.enabled:
        rep = cfg.post.reports
        stmt_globs = rep.statements or cfg.post.balance_statements
        stmt_paths = _expand_globs(stmt_globs, project_root)
        wanted = [
            name for name, on in (
                ("income", rep.income),
                ("concentration", rep.concentration),
                ("net-worth", rep.net_worth),
                ("allocation", rep.allocation),
                ("portfolio-allocation", rep.portfolio_allocation),
            ) if on
        ]
        err_console.print(
            f"[bold]reports[/bold] {', '.join(wanted)} "
            f"({len(stmt_paths)} statement{'s' if len(stmt_paths) != 1 else ''})"
        )
        if not dry_run:
            _run_rebuild_reports(rep, data_dir, stmt_paths, project_root)

    # --- Step 4: reconcile -----------------------------------------------
    # Runs *before* bean-check: bean-check exits nonzero on a drifted
    # assertion, so going first guarantees reconcile's localised report
    # (drift rows + earliest-drift + coverage gaps) is written and
    # printed. Reconcile is a gate in its own right — drift fails the
    # rebuild, and coverage gaps fail it under strict (bean-check can't
    # see a missing assertion).
    if cfg.post.reconcile.enabled:
        rec = cfg.post.reconcile
        rec_ledger = _resolve_ledger(rec.ledger, data_dir)
        rec_balances = _resolve_balances(rec.balances, data_dir)
        rec_strict = rec.strict or strict
        err_console.print(
            f"[bold]reconcile[/bold] {rec_ledger} vs {rec_balances}"
            + (" (strict)" if rec_strict else "")
        )
        if not dry_run:
            out_dir = (
                settings.reconciliation_dir
                if settings.reconciliation_dir.is_absolute()
                else project_root / settings.reconciliation_dir
            )
            report = _run_reconcile(rec_ledger, rec_balances, out_dir)
            if report is not None and (
                report.has_drift or (rec_strict and report.coverage_gaps)
            ):
                raise typer.Exit(code=1)

    # --- Step 5: bean-check validation -----------------------------------
    # Runs last so it sees every freshly-built file. A non-zero exit
    # from bean-check raises typer.Exit so cron / CI notices.
    if cfg.post.check.enabled:
        ledger = _resolve_ledger(cfg.post.check.ledger, data_dir)
        # CLI ``--strict`` overrides the config — when set, escalate
        # bean-check warnings to errors regardless of what
        # ``[post.check] strict`` says.
        check_strict = cfg.post.check.strict or strict
        err_console.print(
            f"[bold]check[/bold] {ledger}"
            + (" (strict)" if check_strict else "")
        )
        if not dry_run:
            _run_check_or_exit(ledger, strict=check_strict)


def _resolve_report_dir(configured: Path, project_root: Path) -> Path:
    """Resolve a configured ``*_reports_dir`` against the project root.

    The report-dir settings default to repo-relative paths (``reports/…``);
    rebuild resolves them against ``project_root`` so the output lands in
    the same place regardless of the process's working directory."""

    if configured.is_absolute():
        return configured
    return (project_root / configured).resolve()


def _write_report(
    out_dir: Path, md_name: str, md_text: str, csv_name: str,
    csv_rows: list[list[str]],
) -> None:
    """Write a report's ``.md`` + ``.csv`` to ``out_dir`` (created if absent)."""

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / md_name).write_text(md_text, encoding="utf-8")
    with (out_dir / csv_name).open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(csv_rows)
    err_console.print(f"  wrote {out_dir}/{md_name} + {csv_name}")


def _run_rebuild_reports(
    rep: ReportsStep,
    data_dir: Path,
    statement_paths: list[Path],
    project_root: Path,
) -> None:
    """Regenerate the analytical reports for the rebuild's reports step.

    Uses the configured GBP rate source / commodity metadata / property
    table (no CLI overrides in rebuild). The valuation reports read the
    statement archive; ``income`` reads the sidecars under ``data_dir``.
    """

    commodities_map = _resolve_commodities() or {}
    rates = build_rate_source(settings)
    properties = _load_properties(None)
    texts = [(_statement_text(p), p.name) for p in statement_paths]

    if rep.income:
        report = income_mod.compute_income(
            _load_sidecar_transactions(data_dir),
            period=cast(income_mod.PeriodMode, rep.income_period),
            commodities=commodities_map, source=rates,
        )
        _write_report(
            _resolve_report_dir(settings.income_reports_dir, project_root),
            "income.md", income_mod.render_markdown(report),
            "income.csv", income_mod.render_csv_rows(report),
        )
    if rep.concentration:
        creport = concentration_mod.build_report(
            texts, commodities=commodities_map, rate_source=rates,
            properties=properties,
        )
        _write_report(
            _resolve_report_dir(settings.concentration_reports_dir, project_root),
            "concentration.md", concentration_mod.render_markdown(creport),
            "holdings.csv", concentration_mod.render_csv_rows(creport),
        )
    if rep.net_worth:
        timeline = net_worth_mod.build_timeline(
            texts, commodities=commodities_map, rate_source=rates,
            properties=properties,
        )
        _write_report(
            _resolve_report_dir(settings.net_worth_reports_dir, project_root),
            "net-worth.md", net_worth_mod.render_markdown(timeline),
            "net-worth.csv", net_worth_mod.render_csv_rows(timeline),
        )
    if rep.allocation:
        atimeline = allocation_mod.build_timeline(
            texts, commodities=commodities_map, rate_source=rates,
            properties=properties,
        )
        _write_report(
            _resolve_report_dir(settings.allocation_reports_dir, project_root),
            "allocation.md", allocation_mod.render_markdown(atimeline),
            "allocation.csv", allocation_mod.render_csv_rows(atimeline),
        )
    if rep.portfolio_allocation:
        preport = portfolio_allocation_mod.build_report(
            texts, commodities=commodities_map, rate_source=rates,
            properties=properties,
        )
        _write_report(
            _resolve_report_dir(
                settings.portfolio_allocation_reports_dir, project_root
            ),
            "portfolio-allocation.md",
            portfolio_allocation_mod.render_markdown(preport),
            "portfolio-allocation.csv",
            portfolio_allocation_mod.render_csv_rows(preport),
        )


def _resolve_ledger(ledger: str, data_dir: Path) -> Path:
    """Resolve a rebuild ledger setting (``[post.check]`` / ``[post.reconcile]``).

    Empty string defaults to ``<data_dir>/portfolio.beancount`` (the
    aggregate the ``portfolio`` step writes — it ``include``s everything
    else, so checking it transitively checks the whole rebuild). An
    explicit value overrides — useful when you have a parent
    ``main.beancount`` that ``include``s the rebuild output alongside
    hand-curated opens / commodities / metadata. Relative paths resolve
    against the project root (``data_dir``'s parent).
    """

    if not ledger:
        return data_dir / "portfolio.beancount"
    explicit = Path(ledger).expanduser()
    if explicit.is_absolute():
        return explicit
    return (data_dir.parent / explicit).resolve()


def _resolve_balances(balances: str, data_dir: Path) -> Path:
    """Resolve a ``[post.reconcile] balances`` setting to a path.

    Empty string defaults to ``<data_dir>/balances.beancount`` (what the
    ``balances`` step writes). Otherwise resolved like
    :func:`_resolve_ledger`.
    """

    if not balances:
        return data_dir / "balances.beancount"
    explicit = Path(balances).expanduser()
    if explicit.is_absolute():
        return explicit
    return (data_dir.parent / explicit).resolve()


def _run_check_or_exit(ledger: Path, *, strict: bool) -> None:
    """Run bean-check and ``typer.Exit`` on failure.

    Missing ledger → fatal (rebuild bug, the file should always exist
    after the portfolio step ran). Missing bean-check binary → warning
    (user opted out of validation by not installing it). Non-zero
    return code → echo bean-check's report verbatim, exit with the
    same code so callers (cron, CI, ``run.sh``) can branch on it.
    """

    if not ledger.exists():
        err_console.print(
            f"[red]bean-check skipped: ledger {ledger} doesn't exist[/red]"
        )
        raise typer.Exit(code=1)

    result = bean_check.run_bean_check(ledger, strict=strict)
    if result.binary_missing:
        err_console.print(f"[yellow]warning:[/yellow] {result.stderr}")
        return
    if result.ok:
        err_console.print("  [green]ok[/green]")
        return
    err_console.print(
        f"[red]bean-check failed (rc={result.returncode}) on {ledger}:[/red]"
    )
    err_console.print(result.stderr, markup=False, highlight=False, soft_wrap=True)
    raise typer.Exit(code=result.returncode or 1)


def _expand_globs(globs: list[str], project_root: Path) -> list[Path]:
    """Expand a list of TOML-supplied glob strings to a flat path list.

    Mirrors :meth:`Source.expand` but for the post-processing steps
    that take a flat list of files rather than a single labelled glob.
    Sorted for stable output and deduplicated so repeated globs don't
    double-count.
    """

    seen: set[Path] = set()
    for glob in globs:
        seen.update(Source(label="_", glob=glob).expand(project_root))
    return sorted(seen)


def _load_sidecar_transactions(source: Path) -> list[Transaction]:
    """Load every ``*.transactions.jsonl`` under ``source`` (recursive)."""

    txns: list[Transaction] = []
    for path in sorted(source.rglob("*.transactions.jsonl")):
        txns.extend(load_transactions(path))
    return txns


