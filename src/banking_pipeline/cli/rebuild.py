"""Rebuild orchestration + validation commands.

``rebuild`` (the end-to-end config-driven run), ``check`` (bean-check
wrapper), and ``reconcile`` (statement-balance drift report), plus the
rebuild step machinery. Shared helpers (statement discovery, commodity /
property / sidecar loading, bean-check) come from
:mod:`banking_pipeline.cli._main`.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Annotated, cast

import typer

from banking_pipeline import (
    allocation as allocation_mod,
)
from banking_pipeline import (
    archive,
    balances_extract,
    bean_check,
    beancount_writer,
    portfolio_aggregate,
    prices_extract,
)
from banking_pipeline import (
    balance_sheet as balance_sheet_mod,
)
from banking_pipeline import (
    concentration as concentration_mod,
)
from banking_pipeline import (
    holdings as holdings_mod,
)
from banking_pipeline import (
    income as income_mod,
)
from banking_pipeline import (
    mandate_benchmark as mandate_benchmark_mod,
)
from banking_pipeline import (
    mandate_returns as mandate_returns_mod,
)
from banking_pipeline import (
    mandate_scorecard as mandate_scorecard_mod,
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
from banking_pipeline import (
    trial_balance as trial_balance_mod,
)
from banking_pipeline.batch_config import (
    BatchConfig,
    ImportStep,
    ReportsStep,
    load_config,
)
from banking_pipeline.cli._main import (
    _configure_logging,
    _expand_globs,
    _filter_priced_statements,
    _load_properties,
    _load_sidecar_transactions,
    _resolve_commodities,
    _resolve_name_to_isin,
    _run_check_or_exit,
    _run_completeness,
    _run_reconcile_transactions,
    _statement_text,
    app,
    err_console,
)
from banking_pipeline.cli_options import (
    VerboseOpt,
)
from banking_pipeline.config import settings
from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.fx.gbp_rates import build_rate_source
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.opening_positions import load_opening_positions
from banking_pipeline.pipeline import Pipeline
from banking_pipeline.tax.uk.basis import UkSection104Lens
from banking_pipeline.tax.uk.eri import cumulative_base_cost_adjustments, load_eri
from banking_pipeline.transaction_sidecar import (
    dump_transactions,
    sidecar_path,
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
            help="Accepted for symmetry with ``ingest`` / ``rebuild``, but "
            "currently has no extra effect: beancount v3's bean-check has "
            "no warnings-as-errors flag (the v2-era ``-w`` is gone) and "
            "reports only errors, which already make the check fail. A "
            "clean ledger passes; any error fails — strict or not.",
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


def _missing_source_guard(
    cfg: BatchConfig,
    project_root: Path,
    data_dir: Path,
    stale: set[Path],
) -> list[tuple[str, str, Path]]:
    """Sources that match zero PDFs but whose output ``clean`` would delete.

    Returns ``(label, glob, output_path)`` for each ``[[sources]]`` entry
    in the data-loss case: the glob currently resolves to nothing *and*
    its ``<label>.beancount`` is in the clean step's delete set — so a
    rebuild would wipe a ledger it then can't regenerate. A source whose
    output doesn't exist yet (a new, not-yet-populated year) is not in
    ``stale`` and so is never flagged. An empty result means it's safe to
    clean.
    """

    offenders: list[tuple[str, str, Path]] = []
    for src in cfg.sources:
        out_path = data_dir / f"{src.label}.beancount"
        if out_path in stale and not src.expand(project_root):
            offenders.append((src.label, src.glob, out_path))
    return offenders


def _run_import(cfg_import: ImportStep, *, dry_run: bool) -> None:
    """File fresh downloads into the dated archive before ingest.

    Resolves the source(s) and archive root from the ``[import]`` config,
    falling back to the ``import_*`` settings exactly as the ``import``
    command does, then runs the same ``archive.file_documents`` filing
    pass. A no-op (warning, not an error) when no source or archive
    resolves, so an ``enabled``-but-unconfigured step doesn't abort the
    whole rebuild. Honours ``dry_run`` (plans are computed but no file
    is moved).
    """

    archive_dir = (
        Path(cfg_import.archive_dir).expanduser()
        if cfg_import.archive_dir
        else settings.import_archive_dir
    )

    # Source resolution mirrors the ``import`` command: a glob (config,
    # then settings) wins over a single dir (config, then settings),
    # since a glob is how the bank's periodic zips arrive.
    if cfg_import.source_glob:
        sources = archive.expand_source_glob(cfg_import.source_glob)
    elif settings.import_source_glob:
        sources = archive.expand_source_glob(settings.import_source_glob)
    elif cfg_import.source_dir:
        sources = [Path(cfg_import.source_dir).expanduser()]
    elif settings.import_source_dir is not None:
        sources = [settings.import_source_dir]
    else:
        sources = []

    # Additional globs (e.g. loose Pictet tax-report PDFs) compose with —
    # rather than replace — the primary source above.
    for pattern in cfg_import.source_globs:
        sources.extend(archive.expand_source_glob(pattern))

    # Portal CSV exports file separately from the PDF sources — a CSV isn't a
    # PDF, so it bypasses the classifier and files by content (keep-latest).
    # Independent of the PDF sources: a run may have any combination.
    csv_sources: list[Path] = []
    for pattern in cfg_import.cash_statement_globs:
        csv_sources.extend(archive.expand_source_glob(pattern))
    transactions_sources: list[Path] = []
    for pattern in cfg_import.transactions_globs:
        transactions_sources.extend(archive.expand_source_glob(pattern))

    if archive_dir is None or (
        not sources and not csv_sources and not transactions_sources
    ):
        err_console.print(
            "[yellow]import skipped:[/yellow] no source/archive resolved "
            "([import] source_glob / source_dir / archive_dir or the "
            "import_* settings)"
        )
        return

    extra = [
        (len(csv_sources), "cash CSV(s)"),
        (len(transactions_sources), "transactions CSV(s)"),
    ]
    err_console.print(
        f"[bold]import[/bold] {len(sources)} source(s)"
        + "".join(f" + {n} {label}" for n, label in extra if n)
        + f" → {archive_dir}"
    )

    plans: list[archive.FilingPlan] = []
    if sources:
        with archive.source_pdfs(sources, cfg_import.pattern) as pdfs:
            plans = archive.file_documents(pdfs, archive_dir, dry_run=dry_run)
    if csv_sources:
        plans += archive.file_cash_statements(
            csv_sources, archive_dir, dry_run=dry_run
        )
    if transactions_sources:
        plans += archive.file_transactions_csv(
            transactions_sources, archive_dir, dry_run=dry_run
        )

    moved = sum(1 for p in plans if p.status == "move")
    skipped = sum(1 for p in plans if p.status == "skip")
    unmatched = sum(1 for p in plans if p.status == "no-match")
    errored = sum(1 for p in plans if p.status == "error")
    err_console.print(
        f"  {moved} {'to file' if dry_run else 'filed'}, {skipped} skipped, "
        f"{unmatched} unmatched, {errored} error(s)"
    )


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
    # The generated aggregate carries every portfolio's account opens, so
    # a reconcilable portfolio present there but absent from the assertions
    # is a whole-portfolio hole (how the P mandate hid). Assumes the
    # aggregate sits beside the balances file (the default layout — both in
    # ``data_dir``); if a custom ``--balances`` path moves it elsewhere the
    # check degrades to a silent skip rather than erroring.
    portfolio_ledger = balances.parent / "portfolio.beancount"
    ledger_portfolios = (
        reconcile_mod.parse_ledger_portfolios(
            portfolio_ledger.read_text(encoding="utf-8")
        )
        if portfolio_ledger.is_file()
        else None
    )
    report = reconcile_mod.build_report(
        assertions, failures, ledger_portfolios=ledger_portfolios
    )

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
            "silently skipping the document; (b) the balances step "
            "reconciles each statement against itself and fails on any "
            "holding / cash row the parser dropped. (The bean-check "
            "post-step fails on any error regardless of --strict; "
            "beancount v3 has no warnings-as-errors flag to escalate.)",
        ),
    ] = False,
    allow_missing_sources: Annotated[
        bool,
        typer.Option(
            "--allow-missing-sources",
            help="Proceed even when a [[sources]] glob matches zero files "
            "and the clean step would delete its existing output. Off by "
            "default: a moved or unsynced source is the likely cause, and "
            "cleaning would wipe data that can't be regenerated. Set this "
            "only when you really do mean to drop that output.",
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

    0. (Optional, ``[import] enabled = true``) files fresh downloads
       into the dated archive tree the ``[[sources]]`` globs read from.
    1. Deletes stale outputs under ``<data_dir>/<clean_glob>``.
    2. Runs ``ingest`` once per ``[[sources]]`` entry, writing to
       ``<data_dir>/<label>.beancount``.
    3. Runs ``prices`` / ``portfolio`` / ``balances`` / ``reports`` /
       ``reconcile`` / ``check`` according to the ``[post]`` toggles.

    A source glob that matches zero files surfaces as a non-fatal
    warning — handy when a year-partition hasn't received any documents
    yet — *unless* the clean step would also delete that source's
    existing output: that's the moved / unsynced-source case (a wiped
    ledger the ingest step can't regenerate), so the rebuild aborts
    before deleting anything. Pass ``--allow-missing-sources`` to drop
    such outputs deliberately. ``--dry-run`` previews every step without
    touching the filesystem; useful before the first real run on a new
    config.
    """

    _configure_logging(verbose)
    # Resolve the default here rather than in the signature: ``Path.cwd()``
    # as a parameter default is evaluated once at import, not per invocation.
    project_root = project_root or Path.cwd()
    cfg = load_config(project_root, config_path=config)
    _do_rebuild(
        cfg,
        project_root=project_root,
        dry_run=dry_run,
        strict=strict,
        allow_missing_sources=allow_missing_sources,
    )


def _do_rebuild(
    cfg: BatchConfig,
    *,
    project_root: Path,
    dry_run: bool,
    strict: bool = False,
    allow_missing_sources: bool = False,
) -> None:
    """Execute (or preview) the steps described by ``cfg``."""

    data_dir = cfg.resolve_data_dir(project_root)
    if not dry_run:
        data_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 0: import ---------------------------------------------------
    # Files fresh downloads into the dated archive *before* the ingest
    # globs read from it, so one rebuild can run end to end. Off by
    # default — keeping it opt-in keeps a plain rebuild idempotent (no
    # file moves on a re-run).
    if cfg.import_step.enabled:
        _run_import(cfg.import_step, dry_run=dry_run)

    # --- Step 1: clean ----------------------------------------------------
    stale = list(cfg.stale_files(project_root))

    # Guard against the data-loss footgun: a source that matches zero PDFs
    # but whose existing output ``clean`` would delete (a moved or unsynced
    # source — e.g. a relocated Dropbox folder). Cleaning then skipping
    # would wipe a ledger that can't be regenerated. A genuinely-new empty
    # year (output absent → not in ``stale``) is unaffected and still just
    # warns in the ingest loop below.
    offenders = _missing_source_guard(cfg, project_root, data_dir, set(stale))
    if offenders:
        listing = "\n".join(
            f"  - {label!r} (glob={glob!r}) → would delete {out}"
            for label, glob, out in offenders
        )
        if allow_missing_sources:
            err_console.print(
                "[yellow]warning:[/yellow] proceeding past sources that "
                "matched zero files; their existing output will be "
                f"deleted:\n{listing}"
            )
        else:
            err_console.print(
                "[red]error:[/red] these sources matched zero files, but "
                "the clean step would delete their existing output and the "
                "ingest step couldn't regenerate it (a moved or unsynced "
                f"source is the likely cause):\n{listing}\n"
                "Nothing has been deleted. Fix the source glob(s), or pass "
                "--allow-missing-sources to clean and skip them anyway."
            )
            raise typer.Exit(code=2)

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
            name_to_isin = _resolve_name_to_isin()
            output_path, total = balances_extract.generate(
                data_dir=data_dir,
                statement_files=statements,
                output=None,
                name_to_isin=name_to_isin,
            )
            err_console.print(
                f"  wrote {output_path} ({total} balance assertion(s))"
            )
            if strict:
                gaps = balances_extract.coverage_report(statements, name_to_isin)
                if gaps:
                    for path, file_gaps in gaps:
                        for gap in file_gaps:
                            err_console.print(
                                f"  [red]coverage gap[/red] {path.name}: "
                                f"{gap.message}"
                            )
                    raise typer.Exit(code=1)
                err_console.print("  coverage check passed")

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
                ("trial-balance", rep.trial_balance),
                ("balance-sheet", rep.balance_sheet),
                ("mandate-scorecard", rep.mandate_scorecard),
                ("mandate-returns", rep.mandate_returns),
                ("benchmark", rep.benchmark),
            ) if on
        ]
        err_console.print(
            f"[bold]reports[/bold] {', '.join(wanted)} "
            f"({len(stmt_paths)} statement{'s' if len(stmt_paths) != 1 else ''})"
        )
        if not dry_run:
            tb_ledger = _resolve_ledger(
                rep.trial_balance_ledger or cfg.post.check.ledger, data_dir
            )
            _run_rebuild_reports(
                rep, data_dir, stmt_paths, project_root, tb_ledger
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
                report.has_drift
                or (
                    rec_strict
                    and (report.coverage_gaps or report.has_missing_portfolio)
                )
            ):
                raise typer.Exit(code=1)

    # --- Step 4b: completeness cross-check -------------------------------
    # Transaction-level counterpart to reconcile's balance-level check:
    # diffs each current-account statement against the sidecars. Runs
    # before ``check`` so its per-statement reports always land. A gate in
    # its own right — MISSING fails the rebuild; UNMATCHED fails it under
    # strict.
    if cfg.post.completeness.enabled:
        comp = cfg.post.completeness
        stmt_paths = _expand_globs(comp.statements, project_root)
        comp_strict = comp.strict or strict
        err_console.print(
            f"[bold]completeness[/bold] {len(stmt_paths)} statement(s)"
            + (" (strict)" if comp_strict else "")
        )
        if not dry_run:
            out_dir = _resolve_report_dir(settings.completeness_dir, project_root)
            missing, unmatched, _written = _run_completeness(
                stmt_paths, data_dir, out_dir
            )
            if missing or (comp_strict and unmatched):
                raise typer.Exit(code=1)

    # --- Step 4c: transaction-level reconciliation -----------------------
    # Diffs the portal Transactions export against the sidecars by Order nr.
    # — covers the securities legs completeness excludes. MISSING and
    # AMOUNT_MISMATCH fail the rebuild; UNMATCHED fails under strict.
    if cfg.post.reconcile_transactions.enabled:
        rtx = cfg.post.reconcile_transactions
        export_paths = _expand_globs(rtx.statements, project_root)
        rtx_strict = rtx.strict or strict
        err_console.print(
            f"[bold]reconcile-transactions[/bold] {len(export_paths)} export(s)"
            + (" (strict)" if rtx_strict else "")
        )
        if not dry_run:
            out_dir = _resolve_report_dir(
                settings.reconcile_transactions_dir, project_root
            )
            missing, unmatched, mismatch, _written = _run_reconcile_transactions(
                export_paths, data_dir, out_dir
            )
            if missing or mismatch or (rtx_strict and unmatched):
                raise typer.Exit(code=1)

    # --- Step 5: bean-check validation -----------------------------------
    # Runs last so it sees every freshly-built file. A non-zero exit
    # from bean-check raises typer.Exit so cron / CI notices.
    if cfg.post.check.enabled:
        ledger = _resolve_ledger(cfg.post.check.ledger, data_dir)
        # ``check_strict`` is threaded through and surfaced in the banner,
        # but beancount v3's bean-check has no warnings-as-errors flag, so
        # it no longer changes the invocation — bean-check fails on any
        # error either way (see ``bean_check.run_bean_check``).
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
    trial_balance_ledger: Path,
) -> None:
    """Regenerate the analytical reports for the rebuild's reports step.

    Uses the configured GBP rate source / commodity metadata / property
    table (no CLI overrides in rebuild). The valuation reports read the
    statement archive; ``income`` reads the sidecars under ``data_dir``;
    ``trial_balance`` queries ``trial_balance_ledger`` via ``bean-query``.
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
    if rep.holdings:
        # ISA trades are UK-tax-exempt → no section 104 basis, so excluded
        # from the lens (mirrors the tax choke point); ISA holdings still show
        # from the statement side with a blank cost. ``rates`` is exact-month
        # (the tax convention); ``value_holdings`` forward-fills it for marks.
        hld_txns = [
            tx for tx in _load_sidecar_transactions(data_dir) if not tx.is_tax_exempt
        ]
        opening = (
            load_opening_positions(settings.opening_positions_path)
            if settings.opening_positions_path is not None
            and settings.opening_positions_path.is_file()
            else {}
        )
        eri_entries = (
            load_eri(settings.eri_path)
            if settings.eri_path is not None and settings.eri_path.is_file()
            else {}
        )
        adjustments, eri_gaps = cumulative_base_cost_adjustments(
            hld_txns, eri_entries=eri_entries, commodities=commodities_map,
            source=rates, opening_positions=opening,
        )
        if eri_gaps:
            err_console.print(
                f"[yellow]holdings: {len(eri_gaps)} ERI entry/entries had no "
                "GBP rate — the cost basis for those holdings omits that "
                "uplift.[/yellow]"
            )
        lens = UkSection104Lens(
            transactions=hld_txns, commodities=commodities_map, source=rates,
            opening_positions=opening, cost_adjustments=adjustments,
        )
        hreport = holdings_mod.build_report(
            texts, commodities=commodities_map, rate_source=rates, basis=lens
        )
        _write_report(
            _resolve_report_dir(settings.holdings_reports_dir, project_root),
            "holdings.md", holdings_mod.render_markdown(hreport),
            "holdings.csv", holdings_mod.render_csv_rows(hreport),
        )
    if rep.net_worth:
        timeline = net_worth_mod.build_timeline(
            texts, commodities=commodities_map, rate_source=rates,
            properties=properties, monthly=rep.net_worth_monthly,
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
    if rep.trial_balance:
        # Ledger-based (bean-query), unlike the statement reports above. A
        # missing ledger / binary, or a ledger that won't load, is a warning
        # + skip, not a failed rebuild — the [post.check] step is what gates
        # the ledger.
        result = (
            trial_balance_mod.query_balances(trial_balance_ledger)
            if trial_balance_ledger.is_file()
            else None
        )
        if result is None:
            err_console.print(
                f"[yellow]trial-balance skipped:[/yellow] ledger "
                f"{trial_balance_ledger} not found"
            )
        elif not result.ok:
            err_console.print(
                f"[yellow]trial-balance skipped:[/yellow] {result.error}"
            )
        else:
            tb = trial_balance_mod.build_trial_balance(
                result, on_date=date.today(), rate_source=rates
            )
            _write_report(
                _resolve_report_dir(
                    settings.trial_balance_reports_dir, project_root
                ),
                "trial-balance.md",
                "\n".join(trial_balance_mod.render_markdown(tb)),
                "trial-balance.csv",
                trial_balance_mod.render_csv_rows(tb),
            )
    if rep.balance_sheet:
        # Ledger-based (bean-query), like trial-balance: build the
        # self-contained HTML artifact from the queried Asset/Liability
        # postings + prices + FX. Missing ledger / binary warns and skips.
        if not trial_balance_ledger.is_file():
            err_console.print(
                f"[yellow]balance-sheet skipped:[/yellow] ledger "
                f"{trial_balance_ledger} not found"
            )
        else:
            bs_data, bs_result = balance_sheet_mod.build_data(
                trial_balance_ledger,
                commodities=commodities_map,
                rate_source=rates,
                prices_path=data_dir / "prices.beancount",
                assertions_path=data_dir / "balances.beancount",
            )
            if bs_data is None:
                err_console.print(
                    f"[yellow]balance-sheet skipped:[/yellow] {bs_result.error}"
                )
            else:
                balance_sheet_mod.write_artifact(
                    bs_data,
                    _resolve_report_dir(
                        settings.balance_sheet_reports_dir, project_root
                    ),
                )
    if rep.mandate_scorecard:
        # Ledger-based (bean-query, like trial-balance) for the costs, plus
        # the statement archive for the average-invested denominator. A
        # missing binary / ledger warns and skips, not a failed rebuild.
        cost_result = mandate_scorecard_mod.query_costs(trial_balance_ledger)
        if not trial_balance_ledger.is_file() or cost_result.binary_missing:
            err_console.print(
                f"[yellow]mandate-scorecard skipped:[/yellow] "
                f"{cost_result.error or f'ledger {trial_balance_ledger} not found'}"
            )
        elif not cost_result.ok:
            err_console.print(
                f"[yellow]mandate-scorecard skipped:[/yellow] {cost_result.error}"
            )
        else:
            ms_timeline = net_worth_mod.build_timeline(
                texts, commodities=commodities_map, rate_source=rates,
                properties=properties,
            )
            cost_report = mandate_scorecard_mod.build_cost_report(
                cost_result, rate_source=rates, timeline=ms_timeline
            )
            _write_report(
                _resolve_report_dir(
                    settings.mandate_scorecard_reports_dir, project_root
                ),
                "mandate-scorecard.md",
                mandate_scorecard_mod.render_markdown(cost_report),
                "mandate-scorecard.csv",
                mandate_scorecard_mod.render_csv_rows(cost_report),
            )
    if rep.mandate_returns:
        # Holdings-based — no ledger / bean-query — but the sidecars supply the
        # distribution income the price-only gain would otherwise miss.
        returns_report = mandate_returns_mod.build_report(
            texts, commodities=commodities_map, rate_source=rates,
            transactions=_load_sidecar_transactions(data_dir),
        )
        _write_report(
            _resolve_report_dir(
                settings.mandate_returns_reports_dir, project_root
            ),
            "mandate-returns.md",
            mandate_returns_mod.render_markdown(returns_report),
            "mandate-returns.csv",
            mandate_returns_mod.render_csv_rows(returns_report),
        )
    if rep.benchmark:
        # Statement-derived mandate periods + the benchmark-levels CSV. A
        # missing CSV warns and skips, not a failed rebuild.
        if settings.benchmark_path is None or not settings.benchmark_path.is_file():
            err_console.print(
                "[yellow]benchmark skipped:[/yellow] no benchmark CSV "
                "(run scripts/fetch_benchmarks.py / set benchmark_path)"
            )
        else:
            periods = mandate_returns_mod.aggregate_period_returns(
                texts, commodities=commodities_map, rate_source=rates,
                transactions=_load_sidecar_transactions(data_dir),
            )
            bench_report = mandate_benchmark_mod.build_report(
                periods, mandate_benchmark_mod.load_benchmarks(settings.benchmark_path)
            )
            _write_report(
                _resolve_report_dir(settings.benchmark_reports_dir, project_root),
                "benchmark-value-add.md",
                mandate_benchmark_mod.render_markdown(bench_report),
                "benchmark-value-add.csv",
                mandate_benchmark_mod.render_csv_rows(bench_report),
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
