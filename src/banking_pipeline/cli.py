"""Typer CLI entrypoint."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console

from banking_pipeline import (
    balances_extract,
    bean_check,
    beancount_writer,
    portfolio_aggregate,
    prices_extract,
)
from banking_pipeline.batch_config import BatchConfig, CheckStep, Source, load_config
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.classifiers.bank import BANK_RULES, BankRuleClassifier
from banking_pipeline.classifiers.language import LANGUAGE_RULES, LanguageRuleClassifier
from banking_pipeline.classifiers.rules import DEFAULT_RULES, RuleClassifier
from banking_pipeline.extractors import extract_pages, load_pdf
from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.models import Classification, DocumentType
from banking_pipeline.pipeline import Pipeline

app = typer.Typer(help="Ingest banking PDFs and emit beancount entries.")
console = Console()
err_console = Console(stderr=True)


def _configure_logging(verbose: bool) -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            20 if not verbose else 10  # INFO vs DEBUG
        ),
    )


@app.command()
def ingest(
    pdf_paths: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write beancount entries to this file instead of stdout."),
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
    for path in pdf_paths:
        try:
            result = pipeline.process(path)
        except TemplateExtractionError as exc:
            err_console.print(
                f"[red]extraction error:[/red] {exc}"
            )
            raise typer.Exit(code=1) from exc
        chunks.append(beancount_writer.render(result))

    rendered = "\n\n".join(chunks)
    if output is None:
        console.print(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        err_console.print(f"Wrote {output}")

    if check is not None:
        err_console.print(f"[bold]check[/bold] {check}")
        _run_check_or_exit(check, strict=strict)


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
    ] = [],
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
    ] = [],
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
    ] = ["GBP"],
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
    output_path, total = portfolio_aggregate.generate(
        data_dir=data_dir,
        output=output,
        operating_currencies=operating_currency,
        booking_method=booking_method or None,
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
        Path,
        typer.Option(
            "--project-root",
            help="Project root used to resolve relative paths and "
            "locate the default config. Defaults to the current "
            "working directory.",
        ),
    ] = Path.cwd(),
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
        out_path.write_text("\n\n".join(chunks), encoding="utf-8")

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

    # --- Step 4: bean-check validation -----------------------------------
    # Runs last so it sees every freshly-built file. A non-zero exit
    # from bean-check raises typer.Exit so cron / CI notices.
    if cfg.post.check.enabled:
        ledger = _resolve_check_ledger(cfg.post.check, data_dir)
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


def _resolve_check_ledger(check: CheckStep, data_dir: Path) -> Path:
    """Pick the ledger entry-point bean-check should validate.

    Defaults to ``<data_dir>/portfolio.beancount`` (the aggregate the
    ``portfolio`` step writes — it ``include``s everything else, so
    checking it transitively checks the whole rebuild). An explicit
    ``[post.check] ledger = "..."`` overrides — useful when you have
    a parent ``main.beancount`` that ``include``s the rebuild output
    alongside hand-curated opens / commodities / metadata.
    """

    if not check.ledger:
        return data_dir / "portfolio.beancount"
    explicit = Path(check.ledger).expanduser()
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


if __name__ == "__main__":
    app()
