"""Typer CLI entrypoint."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console

from banking_pipeline import (
    balances_extract,
    bean_check,
    beancount_writer,
    dedup,
    portfolio_aggregate,
    prices_extract,
)
from banking_pipeline import (
    reconcile as reconcile_mod,
)
from banking_pipeline.batch_config import BatchConfig, Source, load_config
from banking_pipeline.cgt_losses import load_cgt_brought_forward_losses
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.classifiers.bank import BANK_RULES, BankRuleClassifier
from banking_pipeline.classifiers.language import LANGUAGE_RULES, LanguageRuleClassifier
from banking_pipeline.classifiers.rules import DEFAULT_RULES, RuleClassifier
from banking_pipeline.commodities_metadata import CommodityMetadata, load_commodities
from banking_pipeline.config import settings
from banking_pipeline.extractors import extract_pages, load_pdf
from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.fx.gbp_rates import build_rate_source
from banking_pipeline.models import Classification, DocumentType, Transaction
from banking_pipeline.opening_positions import load_opening_positions
from banking_pipeline.pipeline import Pipeline
from banking_pipeline.revolut import import_csvs as revolut_import_csvs
from banking_pipeline.revolut import render as revolut_render
from banking_pipeline.revolut.render import render_open_directives as revolut_open_directives
from banking_pipeline.tax.uk.cgt_allowance import (
    CGT_STATUSES,
    CgtAllowanceResult,
    loss_carryforward_chain,
)
from banking_pipeline.tax.uk.eri import EriResult, compute_eri, load_eri
from banking_pipeline.tax.uk.sa106 import Sa106Report, compute_sa106_dividends
from banking_pipeline.tax.uk.sa108 import Sa108Report, compute_sa108, match_history
from banking_pipeline.tax.uk.tax_year import tax_year_bounds
from banking_pipeline.transaction_sidecar import (
    dump_transactions,
    load_transactions,
    sidecar_path,
    transactions_to_jsonl,
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
def ingest(
    pdf_paths: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write beancount entries to this file instead of stdout.",
        ),
    ] = None,
    check: Annotated[
        Path | None,
        typer.Option(
            "--check",
            help="After writing, run ``bean-check`` against this ledger "
            "to validate that the new entries don't break it. The ledger "
            "must already ``include`` the output file (or the output is "
            "the ledger itself). Exits with bean-check's return code on "
            "failure. Requires ``--output`` to be set; ``--check`` against "
            "stdout is meaningless.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            "-s",
            help="Fail on quality issues instead of warning. Two effects: "
            "(a) when a per-template extractor returns zero transactions "
            "for a doctype that should produce output, raise instead of "
            "silently skipping the document; (b) when --check is set, "
            "treat bean-check warnings as errors.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Classify and extract one or more PDFs, then render beancount entries."""

    _configure_logging(verbose)

    if check is not None and output is None:
        err_console.print(
            "[red]error:[/red] --check requires --output (validation "
            "against stdout is meaningless)"
        )
        raise typer.Exit(code=2)

    pipeline = Pipeline(extractor=HybridExtractor(strict=strict))

    chunks: list[str] = []
    all_txns: list[Transaction] = []
    for path in pdf_paths:
        try:
            result = pipeline.process(path)
        except TemplateExtractionError as exc:
            err_console.print(
                f"[red]extraction error:[/red] {exc}"
            )
            raise typer.Exit(code=1) from exc
        chunks.append(beancount_writer.render(result))
        all_txns.extend(result.transactions)

    rendered = "\n\n".join(chunks)

    # Append ``close`` directives for ISIN-keyed asset accounts whose
    # final units balance across the batch is exactly zero. Detection
    # is conservative — if a position closes here but reopens in a
    # later run, no close is emitted (this batch's net would be > 0).
    closes = beancount_writer.render_close_directives(rendered)
    if closes:
        rendered = f"{rendered}\n\n{closes}\n"

    if output is None:
        console.print(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        err_console.print(f"Wrote {output}")
        # Structured sidecar alongside the ledger — part of the output
        # contract, no flag needed. Combined files carry no single
        # source_document (each line keeps its own source_path).
        sidecar = sidecar_path(output)
        dump_transactions(all_txns, sidecar)
        err_console.print(
            f"Wrote {sidecar} ({len(all_txns)} transaction(s))"
        )

    if check is not None:
        err_console.print(f"[bold]check[/bold] {check}")
        _run_check_or_exit(check, strict=strict)


@app.command("dump-transactions")
def dump_transactions_cmd(
    pdf_paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            readable=True,
            help="One or more PDFs to extract and print as JSONL.",
        ),
    ],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Extract transactions from PDFs and print the JSONL sidecar to stdout.

    The same structured form ``ingest`` / ``rebuild`` write next to each
    ``.beancount``, but emitted to stdout for ad-hoc inspection or for
    piping into the tax-report tooling without touching the on-disk
    ledger. ``source_document`` is set when a single PDF is passed.
    """

    _configure_logging(verbose, quiet=True)
    pipeline = Pipeline()
    txns: list[Transaction] = []
    for path in pdf_paths:
        txns.extend(pipeline.process(path).transactions)
    source = str(pdf_paths[0]) if len(pdf_paths) == 1 else None
    # markup=False/highlight=False keep the JSON byte-exact for piping.
    console.print(
        transactions_to_jsonl(txns, source_document=source),
        markup=False,
        highlight=False,
        end="",
    )


@app.command("dedup-check")
def dedup_check(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Directory walked recursively for *.transactions.jsonl "
            "sidecars. Defaults to ``data``.",
        ),
    ] = Path("data"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write the duplicate rows to this CSV file (one row per "
            "member). Omit to only print the summary.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Audit the transaction sidecars for double-counted events.

    Reads every ``*.transactions.jsonl`` under ``source`` and groups
    transactions that share a content key (date + signed amount +
    currency + ISIN + doctype + account — deliberately *not* the
    per-document reference, so the same event from two documents
    collides). A group with more than one member is a suspected
    duplicate: ``EXACT`` when the members share one document reference
    (the same advice ingested twice), ``POSSIBLE`` otherwise (two
    documents, or refs the extractor couldn't read — review these).

    Read-only — it never touches the ledger. Exits nonzero when any
    duplicate is found, so cron / CI can gate on a clean audit.
    """

    _configure_logging(verbose)

    sidecars = sorted(source.rglob("*.transactions.jsonl"))
    members: list[dedup.DuplicateMember] = []
    for path in sidecars:
        for tx in load_transactions(path):
            members.append(dedup.DuplicateMember(transaction=tx, sidecar=path))

    groups = dedup.find_duplicates(members)
    summary = dedup.render_summary(
        groups, scanned=len(members), sidecars=len(sidecars)
    )
    err_console.print(summary, markup=False, highlight=False, soft_wrap=True)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(dedup.render_csv(groups), encoding="utf-8")
        err_console.print(f"Wrote {output} ({len(groups)} duplicate group(s))")

    if groups:
        raise typer.Exit(code=1)


@app.command()
def revolut(
    csv_paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            readable=True,
            help="One or more Revolut Personal CSV exports. Pass every "
            "currency pocket together so EXCHANGE legs can be paired across "
            "files into balanced two-posting transactions.",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write beancount entries here instead of stdout."),
    ] = None,
    open_directives: Annotated[
        bool,
        typer.Option(
            "--open-directives",
            help="Prepend ``open`` directives for every Assets:Revolut:* "
            "account encountered. One-shot bootstrap for fresh ledgers.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Convert Revolut Personal CSV exports into beancount transactions."""

    _configure_logging(verbose)
    txns = revolut_import_csvs(csv_paths)
    body = revolut_render(txns)
    rendered = (revolut_open_directives(txns) + "\n" if open_directives else "") + body
    if output is None:
        console.print(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        err_console.print(f"Wrote {output} ({len(txns)} txns)")


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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
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
    3. Runs ``prices`` / ``portfolio`` / ``balances`` according to the
       ``[post]`` toggles.

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


@app.command()
def classify(
    pdf_paths: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Classify PDFs without running the full extraction — useful for triage."""

    _configure_logging(verbose)
    pipeline = Pipeline()

    for path in pdf_paths:
        result = pipeline.process(path)
        c = result.classification
        lang_part = (
            f" [dim](lang={c.language.language} "
            f"conf={c.language.confidence:.2f} via {c.language.source})[/dim]"
            if c.language
            else ""
        )
        bank_part = (
            f" [dim](bank={c.bank.bank} "
            f"conf={c.bank.confidence:.2f} via {c.bank.source})[/dim]"
            if c.bank
            else ""
        )
        console.print(
            f"[bold]{path.name}[/bold] → {c.document_type} "
            f"(confidence={c.confidence:.2f}, via {c.source}){lang_part}{bank_part}"
        )


@app.command()
def scan(
    directory: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Directory to scan. By default only the top level is scanned; "
            "pass --recursive to descend into subdirectories.",
        ),
    ],
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive",
            "-r",
            help="Descend into subdirectories. Off by default so an accidental "
            "``scan ~`` doesn't spider the whole home folder.",
        ),
    ] = False,
    pattern: Annotated[
        str,
        typer.Option(
            "--pattern",
            help="Glob pattern for files to classify. Case-insensitive match on the "
            "extension; the default picks up both .pdf and .PDF.",
        ),
    ] = "*.pdf",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit one JSON object per PDF (JSON Lines). Errors are emitted as "
            "`{\"path\": ..., \"error\": ...}` rows instead of classification rows.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write results here instead of stdout."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Classify every PDF in ``directory`` and log the verdict.

    By default only the top level of ``directory`` is scanned — pass
    ``--recursive`` / ``-r`` to walk nested subdirectories as well. The
    classifier runs end-to-end (language → bank → document type) via
    :class:`LayeredClassifier`, but the heavier field extractor is skipped —
    the goal here is fast triage over a folder of mixed statements, not full
    beancount rendering. One file failing (unreadable PDF, etc.) never aborts
    the scan: the error is reported for that row and the walk continues.
    """

    _configure_logging(verbose)
    classifier = LayeredClassifier()

    # Case-insensitive walk: PDFs in the wild arrive as .pdf, .PDF, and
    # occasionally .Pdf, and ``glob``/``rglob`` are case-sensitive on Linux.
    # We union the user's pattern with its upper/lower-cased variants so the
    # obvious default (``*.pdf``) picks up everything without making the user
    # OR-glob by hand. ``recursive`` flips between ``glob`` (top-level only —
    # the safer default so an accidental ``scan ~`` doesn't spider the home
    # folder) and ``rglob`` (descend into every subdirectory).
    walk = directory.rglob if recursive else directory.glob
    seen: set[Path] = set()
    for pat in {pattern, pattern.lower(), pattern.upper()}:
        for candidate in walk(pat):
            if candidate.is_file():
                seen.add(candidate)
    pdfs = sorted(seen)

    lines: list[str] = []
    total = errors = 0
    for pdf_path in pdfs:
        total += 1
        try:
            doc = load_pdf(pdf_path)
            classification = classifier.classify(doc)
        except Exception as exc:  # noqa: BLE001 — report-and-continue is the point
            errors += 1
            line = _format_error(pdf_path, directory, exc, as_json=as_json)
        else:
            line = _format_classification(
                pdf_path, directory, classification, as_json=as_json
            )
        lines.append(line)
        # Stream progress to the user when writing to a file; otherwise the
        # final print does the job and duplicate output is noise.
        # ``soft_wrap=True`` keeps each row on a single output line so JSONL
        # consumers (and CliRunner-driven tests) don't see records split at
        # the terminal width — Rich's default is to wrap at 80 cols when
        # stdout isn't a TTY, which mangles long JSON lines.
        if output is not None:
            err_console.print(line, markup=False, highlight=False, soft_wrap=True)

    rendered = "\n".join(lines)
    if output is None:
        if rendered:
            console.print(rendered, markup=False, highlight=False, soft_wrap=True)
    else:
        output.write_text(rendered + ("\n" if rendered else ""), encoding="utf-8")
        err_console.print(f"Wrote {output}")

    if total == 0:
        err_console.print(f"No files matching {pattern!r} under {directory}")
    else:
        err_console.print(
            f"Scanned {total} file(s); {errors} error(s)."
            if errors
            else f"Scanned {total} file(s)."
        )


def _format_classification(
    pdf_path: Path,
    root: Path,
    classification: Classification,
    *,
    as_json: bool,
) -> str:
    """Render a single classification row in either JSONL or human format."""
    rel = _relative_path(pdf_path, root)
    lang = classification.language
    bank = classification.bank

    if as_json:
        payload = {
            "path": str(rel),
            "language": (
                {
                    "value": lang.language.value,
                    "confidence": round(lang.confidence, 4),
                    "source": lang.source,
                }
                if lang
                else None
            ),
            "bank": (
                {
                    "value": bank.bank.value,
                    "confidence": round(bank.confidence, 4),
                    "source": bank.source,
                }
                if bank
                else None
            ),
            "document_type": {
                "value": classification.document_type.value,
                "confidence": round(classification.confidence, 4),
                "source": classification.source,
                "template_id": classification.template_id,
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    # Compact single-line form: easy to grep, easy to scan visually, and each
    # field is printed the same width regardless of whether it fired so that
    # rows line up in a terminal.
    lang_cell = (
        f"{lang.language.value:>7} {lang.confidence:.2f}" if lang else "      —    —"
    )
    bank_cell = (
        f"{bank.bank.value:>7} {bank.confidence:.2f}" if bank else "      —    —"
    )
    doc_cell = (
        f"{classification.document_type.value:>20} "
        f"{classification.confidence:.2f}"
    )
    return f"[{lang_cell} | {bank_cell} | {doc_cell}] {rel}"


def _format_error(pdf_path: Path, root: Path, exc: Exception, *, as_json: bool) -> str:
    rel = _relative_path(pdf_path, root)
    if as_json:
        return json.dumps(
            {"path": str(rel), "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
    return f"[ERROR {type(exc).__name__:>34}] {rel}: {exc}"


def _relative_path(pdf_path: Path, root: Path) -> Path:
    """Return ``pdf_path`` relative to ``root`` when possible, else unchanged.

    ``rglob`` always yields paths under ``root`` so this is a no-op in practice,
    but ``is_relative_to`` + fallback keeps the function robust if the walker
    ever starts resolving symlinks or following absolute paths.
    """
    try:
        return pdf_path.relative_to(root)
    except ValueError:
        return pdf_path


@app.command("extract-text")
def extract_text(
    pdf_paths: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write text to this file. Defaults to stdout. For multiple PDFs, writes to "
            "<stem>.txt alongside each PDF instead when this is a directory.",
        ),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Omit page/file separators. Useful for piping to grep."),
    ] = False,
    show_rules: Annotated[
        bool,
        typer.Option(
            "--show-rules",
            help="After each document, print which rule patterns matched. Helps author new rules.",
        ),
    ] = False,
) -> None:
    """Dump the extracted plain text from one or more PDFs.

    Handy while authoring new classifier rules: run this on a sample, eyeball
    distinctive phrases, then encode them as regexes in
    ``banking_pipeline.classifiers.rules``.
    """

    rule_classifier = RuleClassifier() if show_rules else None

    # Directory output mode: write one .txt per input PDF.
    if output is not None and output.is_dir():
        for pdf_path in pdf_paths:
            target = output / f"{pdf_path.stem}.txt"
            target.write_text(_render_pdf_text(pdf_path, raw=raw), encoding="utf-8")
            err_console.print(f"Wrote {target}")
            if rule_classifier is not None:
                _print_rule_matches(pdf_path, rule_classifier)
        return

    # Single-stream output (stdout or a single file).
    chunks: list[str] = []
    for pdf_path in pdf_paths:
        if not raw and len(pdf_paths) > 1:
            chunks.append(f"===== {pdf_path} =====")
        chunks.append(_render_pdf_text(pdf_path, raw=raw))

    rendered = "\n\n".join(chunks)
    if output is None:
        console.print(rendered, markup=False, highlight=False)
    else:
        output.write_text(rendered, encoding="utf-8")
        err_console.print(f"Wrote {output}")

    if rule_classifier is not None:
        for pdf_path in pdf_paths:
            _print_rule_matches(pdf_path, rule_classifier)


def _render_pdf_text(pdf_path: Path, *, raw: bool) -> str:
    pages = extract_pages(pdf_path)
    if raw:
        return "\n".join(pages)
    return "\n\n".join(
        f"--- page {i + 1}/{len(pages)} ---\n{page}" for i, page in enumerate(pages)
    )


def _print_rule_matches(pdf_path: Path, classifier: RuleClassifier) -> None:
    """Show which patterns fired at each classification stage."""
    doc = load_pdf(pdf_path)
    err_console.print(f"\n[bold]rule matches for {pdf_path.name}[/bold]")

    # Stage 1: language detection — total stopword hits per language.
    err_console.print("  [bold]language stage[/bold]")
    for lrule in LANGUAGE_RULES:
        total = sum(len(p.findall(doc.text)) for p in lrule.patterns)
        hits = sum(1 for p in lrule.patterns if p.search(doc.text))
        err_console.print(
            f"    {lrule.language}: {hits}/{len(lrule.patterns)} distinct "
            f"stopwords, {total} total occurrences"
        )
    language = LanguageRuleClassifier().classify(doc)
    err_console.print(
        f"  [bold]→ language chosen: {language.language} "
        f"(conf={language.confidence:.2f})[/bold]"
    )

    # Stage 2: bank identification.
    err_console.print("  [bold]bank stage[/bold]")
    for brule in BANK_RULES:
        matches = [p.pattern for p in brule.patterns if p.search(doc.text)]
        if matches:
            err_console.print(
                f"    [green]hit[/green] {brule.bank} — "
                f"{len(matches)}/{len(brule.patterns)} patterns: {matches}"
            )
        else:
            err_console.print(f"    [dim]miss {brule.bank}[/dim]")

    bank = BankRuleClassifier().classify(doc).bank
    err_console.print(f"  [bold]→ bank chosen: {bank}[/bold]")

    # Stage 3: document-type rules.
    err_console.print("  [bold]doc-type stage[/bold]")
    for rule in classifier.rules:
        scope = rule.bank or "generic"
        matches = [p.pattern for p in rule.patterns if p.search(doc.text)]
        if matches:
            err_console.print(
                f"    [green]hit[/green] {rule.doc_type} ({rule.template_id}, bank={scope}) "
                f"— {len(matches)}/{len(rule.patterns)} patterns: {matches}"
            )
        else:
            err_console.print(
                f"    [dim]miss {rule.doc_type} ({rule.template_id}, bank={scope})[/dim]"
            )
    # Reference default ruleset so it's a linker-error if we ever rename it.
    _ = DEFAULT_RULES


# --- tax-report -------------------------------------------------------------

def _load_sidecar_transactions(source: Path) -> list[Transaction]:
    """Load every ``*.transactions.jsonl`` under ``source`` (recursive)."""

    txns: list[Transaction] = []
    for path in sorted(source.rglob("*.transactions.jsonl")):
        txns.extend(load_transactions(path))
    return txns


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


def _write_tax_summary(
    path: Path,
    year: str,
    sa108: Sa108Report,
    sa106: Sa106Report,
    eri: EriResult,
    allowance: CgtAllowanceResult,
    rate_change_date: date | None = None,
    aea_missing: bool = False,
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
        lines.append("")
    if sa108.unmatched_isins:
        lines.append(
            "WARN disposed more than acquired — add opening positions to "
            "data/opening-positions.toml (shortfall matched at zero cost):"
        )
        for isin in sa108.unmatched_isins:
            lines.append(f"  {isin}")
        lines.append("")
    missing = sorted(
        set(sa108.missing_rate_isins)
        | set(sa106.missing_rate_isins)
        | set(eri.missing_rate_isins)
    )
    if missing:
        lines.append("WARN missing GBP rate — excluded from the report:")
        for isin in missing:
            lines.append(f"  {isin}")
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
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

    out_dir = out if out is not None else settings.tax_reports_dir / year
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
        load_eri(eri_path)
        if eri_path is not None and eri_path.is_file()
        else {}
    )

    # Single choke point for tax exemption: drop every transaction sitting
    # in a tax-sheltered wrapper (an ISA today — see TAX_EXEMPT_WRAPPERS)
    # before any CGT / dividend / interest / ERI computation. An ISA's
    # disposals and income are tax-free, so they must never reach SA108 /
    # SA106 or the loss-carry-forward chain.
    txns = [tx for tx in _load_sidecar_transactions(source) if not tx.is_tax_exempt]
    eri_result = compute_eri(
        txns,
        tax_year_label=year,
        eri_entries=eri_entries,
        commodities=commodities_map,
        opening_positions=opening,
        source=rates,
    )
    sa108 = compute_sa108(
        txns,
        tax_year_label=year,
        commodities=commodities_map,
        source=rates,
        rate_change_date=settings.cgt_rate_change_dates.get(year),
        opening_positions=opening,
        cost_adjustments=eri_result.base_cost_adjustments,
    )
    sa106 = compute_sa106_dividends(
        txns, tax_year_label=year, commodities=commodities_map, source=rates
    )

    # CGT annual exempt amount + loss carry-forward: run the matcher over
    # the full history and thread allowable losses across tax years up to
    # the requested one, seeded by any pre-ledger brought-forward losses.
    losses_path = settings.cgt_losses_path
    pre_ledger_losses = (
        load_cgt_brought_forward_losses(losses_path)
        if losses_path is not None and losses_path.is_file()
        else Decimal(0)
    )
    history = match_history(
        txns,
        commodities=commodities_map,
        source=rates,
        opening_positions=opening,
        cost_adjustments=eri_result.base_cost_adjustments,
    )
    chain = loss_carryforward_chain(
        history.rows,
        through_year=year,
        aea_by_year=settings.cgt_annual_exempt_amount,
        rate_change_dates=settings.cgt_rate_change_dates,
        pre_ledger_losses=pre_ledger_losses,
    )
    allowance = chain[year]
    aea_missing = year not in settings.cgt_annual_exempt_amount

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
    _write_tax_summary(
        out_dir / "summary.txt", year, sa108, sa106, eri_result, allowance,
        rate_change_date=settings.cgt_rate_change_dates.get(year),
        aea_missing=aea_missing,
    )

    err_console.print(
        f"Wrote tax report for {year} to {out_dir} "
        f"({n_cgt} SA108 disposal(s), {n_div} SA106 dividend group(s), "
        f"{n_int} SA106 interest group(s), "
        f"{n_oig} offshore income gain(s), {n_dds} deep-discounted disposal(s), "
        f"{n_eri} ERI group(s))"
    )


if __name__ == "__main__":
    app()
