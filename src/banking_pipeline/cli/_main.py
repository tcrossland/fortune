"""Shared CLI infrastructure.

Defines the Typer ``app`` plus the cross-cutting helpers the command
modules share (logging setup, statement discovery / text loading, commodity
/ property / sidecar loading, ledger resolution, the bean-check runner).
The commands themselves live in the sibling group modules (``ingest``,
``inspect``, ``statements``, ``reports``, ``rebuild``, ``tax``), which
import ``app`` and these helpers from here and register their
``@app.command``s on it; :mod:`banking_pipeline.cli` (the package
``__init__``) imports those modules to assemble the full app.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import structlog
import typer
from rich.console import Console

from banking_pipeline import (
    bean_check,
    prices_extract,
    statement_completeness,
    transactions_export,
)
from banking_pipeline.batch_config import (
    Source,
    load_config,
)
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.commodities_metadata import (
    CommodityMetadata,
    build_statement_name_index,
    load_commodities,
)
from banking_pipeline.config import settings
from banking_pipeline.extractors import load_pdf
from banking_pipeline.fx.gbp_rates import GbpRateSource, build_rate_source
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.property import Property, load_properties
from banking_pipeline.transaction_sidecar import (
    load_transactions,
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
    statements_glob: str | None = None,
    latest_only: bool = False,
) -> tuple[list[tuple[str, str]], dict[str, CommodityMetadata], GbpRateSource]:
    """Resolve the inputs shared by the statement-valuation reports
    (``concentration`` / ``net-worth``): discover + load statement texts,
    the commodity metadata, and the GBP rate source. Exits (code 2) when no
    statements are given.

    ``statements_glob`` narrows the ``--statements-dir`` walk to filenames
    matching that glob before any PDF is opened — the fast path over a large
    archive (e.g. ``*monthly*.pdf``). ``None`` keeps the default ``*.pdf``
    (open + classify every PDF).

    ``latest_only`` prunes each ``--statements-dir`` directory to its newest
    statement(s) by filename date *before* opening them — for the reports
    that use only the latest snapshot per portfolio (``holdings``). Explicit
    ``--statement`` files are always kept verbatim.

    With neither ``--statement`` nor ``--statements-dir``, falls back to the
    configured ``balance_statements`` globs (see
    :func:`_configured_statement_paths`) so an ad-hoc report matches the
    rebuild's canonical statement set without hand-listing files."""

    paths = list(statements)
    if statements_dir is not None:
        discovered = _discover_priced_statements(
            statements_dir,
            recursive=statements_recursive,
            pattern=statements_glob or "*.pdf",
            latest_only=latest_only,
        )
        paths += list(discovered)
        err_console.print(
            f"[dim]Discovered {len(discovered)} statement(s) under "
            f"{statements_dir}[/dim]"
        )
    elif not paths:
        configured = _configured_statement_paths()
        if latest_only:
            configured = _latest_statements_per_group(configured)
        paths += configured
        if configured:
            err_console.print(
                f"[dim]No statements given — using {len(configured)} "
                f"configured balance_statements from "
                f"{Path.cwd() / 'banking-pipeline.toml'}[/dim]"
            )
    if not paths:
        err_console.print(
            "[red]No statements given — pass --statement / --statements-dir, "
            "or configure balance_statements in banking-pipeline.toml.[/red]"
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


_STATEMENT_DATE_RE = re.compile(r"(\d{8})")


def _latest_statements_per_group(paths: Iterable[Path]) -> list[Path]:
    """Keep only the newest statement(s) per parent directory, ranked by the
    ``YYYYMMDD`` in the filename.

    The 'latest snapshot' reports (``holdings``) use only the most recent
    statement per portfolio, and Pictet files each portfolio's monthly series
    in its own directory (``<year>/<K|P>-<acct>/reports/
    Valuation-monthly-YYYYMMDD.pdf``), so an older-dated file in the same
    directory can never be the latest and needn't be opened. All files sharing
    a directory's max date are kept (a directory that marks two portfolios does
    so at the same month-end); a file with no parseable date is kept (it can't
    be ranked, so fall back to classifying it). This is a pre-open prune — the
    content-based latest-per-portfolio selection still runs downstream, so a
    directory that mixes portfolios with *different* latest dates degrades to
    slower, never to wrong… as long as one directory holds one portfolio's
    series, which is the archive's layout and what the rebuild globs assume.
    """

    max_date: dict[Path, str] = {}
    for p in paths:
        m = _STATEMENT_DATE_RE.search(p.name)
        if m and m.group(1) > max_date.get(p.parent, ""):
            max_date[p.parent] = m.group(1)
    kept: list[Path] = []
    for p in paths:
        m = _STATEMENT_DATE_RE.search(p.name)
        if m is None or m.group(1) == max_date.get(p.parent):
            kept.append(p)
    return kept


def _discover_priced_statements(
    directory: Path,
    *,
    recursive: bool,
    pattern: str = "*.pdf",
    latest_only: bool = False,
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
    candidates = sorted(seen_paths)
    if latest_only:
        # Prune to the newest statement(s) per directory *before* opening any
        # PDF — the latest-snapshot reports (holdings) only use the most recent
        # per portfolio, so classifying the superseded ones is wasted I/O.
        candidates = _latest_statements_per_group(candidates)
    return _filter_priced_statements(candidates)


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


def _resolve_name_to_isin() -> dict[str, str]:
    """Statement-name → ISIN index for the Pictet P mandate's by-name
    holdings, built from the configured commodity metadata. Empty when no
    metadata is configured (the by-name holdings then stay unresolved and
    the coverage guard flags them)."""

    commodities = _resolve_commodities()
    if not commodities:
        return {}
    return build_statement_name_index(commodities)


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


def _configured_statement_paths() -> list[Path]:
    """Expand the rebuild's ``[post] balance_statements`` globs (from
    ``banking-pipeline.toml`` in the cwd) to a flat path list — the canonical
    statement set the rebuild's report step feeds the valuation reports.

    This is the zero-config default for the ad-hoc report CLIs: it reproduces
    what ``rebuild`` uses (Pictet monthly + the whole Vanguard ISA dir, so the
    ISA isn't silently dropped the way a bare ``*monthly*`` glob would), and
    it's filename-glob expansion (fast — no classify-every-PDF walk). Returns
    ``[]`` when no config file is present, so the caller falls through to its
    "no statements given" error exactly as before."""

    project_root = Path.cwd()
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        return []
    return _expand_globs(cfg.post.balance_statements, project_root)


def _load_sidecar_transactions(source: Path) -> list[Transaction]:
    """Load every ``*.transactions.jsonl`` under ``source`` (recursive)."""

    txns: list[Transaction] = []
    for path in sorted(source.rglob("*.transactions.jsonl")):
        txns.extend(load_transactions(path))
    return txns


def _load_sidecar_rows(source: Path) -> list[dict[str, object]]:
    """Read every ``*.transactions.jsonl`` row under ``source`` as a dict.

    The completeness diff matches on raw sidecar fields, so it consumes the
    JSONL verbatim rather than via the :class:`Transaction` model. The
    per-file schema-header line (carrying ``_schema``) is skipped.
    """

    rows: list[dict[str, object]] = []
    for path in sorted(source.rglob("*.transactions.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            if isinstance(obj, dict) and "_schema" not in obj:
                rows.append(obj)
    return rows


def _run_completeness(
    statement_paths: list[Path], sidecar_dir: Path, out_dir: Path
) -> tuple[int, int, int]:
    """Diff each statement against the sidecars; write per-date reports.

    Shared by the ``completeness`` command and the ``rebuild`` post-step so
    both behave identically (cf. ``_run_reconcile``). Writes one
    ``summary-<key>.txt`` + ``findings-<key>.csv`` per statement, keyed by
    ``<portfolio>-<period-end>`` so neither a repeated run nor two
    portfolios sharing a period clobber each other. Returns
    ``(total_missing, total_unmatched, written)`` and leaves the
    fail-or-not decision to the caller.

    A statement whose running balance won't reconcile raises
    :class:`~banking_pipeline.statement_completeness.StatementParseError`
    (surfaced as a printed ``typer.Exit``); one with no current-account
    section is warned and skipped.
    """

    rows = _load_sidecar_rows(sidecar_dir)
    # The portal CSV's ``Account nr.`` omits the K-/P- mandate letter; resolve
    # each group's portfolio to the lettered form the sidecars use so the diff
    # filter and the report key match the PDF path.
    lettered = statement_completeness.lettered_portfolio_map(rows)
    total_missing = total_unmatched = written = 0
    for path in statement_paths:
        try:
            groups = _completeness_groups(path)
        except statement_completeness.StatementParseError as exc:
            err_console.print(
                f"[red]{path.name}: statement parse failed — {exc}[/red]"
            )
            raise typer.Exit(code=1) from exc
        if not groups:
            err_console.print(
                f"[yellow]{path.name}: no current-account section found "
                "— skipped[/yellow]"
            )
            continue
        for portfolio, lines, period in groups:
            if not statement_completeness.portfolio_is_known(portfolio, lettered):
                # No sidecar account matches this portfolio, so every line will
                # read MISSING (the diff filters out all lettered rows). That's
                # the correct signal — its advices genuinely aren't ingested —
                # but warn so "whole mandate un-ingested" is distinguishable
                # from "a few missing advices". Still diffed + gated below.
                err_console.print(
                    f"[yellow]{path.name}: portfolio {portfolio} has no ingested "
                    f"sidecars — its {len(lines)} line(s) will read MISSING[/yellow]"
                )
            portfolio = statement_completeness.resolve_portfolio(
                portfolio, lettered
            )
            report = statement_completeness.diff(
                lines, rows, period=period, portfolio=portfolio
            )
            # Key on portfolio + period end so two portfolios sharing a period
            # (or a repeated run) each get their own report, never clobbering.
            key = f"{portfolio}-{period[1] if period is not None else path.stem}"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"summary-{key}.txt").write_text(
                statement_completeness.render_summary(path.name, report),
                encoding="utf-8",
            )
            (out_dir / f"findings-{key}.csv").write_text(
                statement_completeness.render_csv(path.name, report),
                encoding="utf-8",
            )
            total_missing += len(report.missing_in_ledger)
            total_unmatched += len(report.unmatched_in_ledger)
            written += 1
    return total_missing, total_unmatched, written


def _completeness_groups(
    path: Path,
) -> list[tuple[str, list[statement_completeness.CashLine], tuple[str, str] | None]]:
    """Resolve one statement path to ``(portfolio, cash_lines, period)`` groups.

    A portal ``.csv`` export holds every mandate, so it yields one group per
    portfolio (period synthesised from its value dates); a ``Financial-
    statement`` PDF / ``.txt`` dump is single-portfolio, single-period, so it
    yields one group. May raise
    :class:`~banking_pipeline.statement_completeness.StatementParseError`.
    """

    if path.suffix.lower() == ".csv":
        lines = statement_completeness.parse_cash_statement_csv(path)
        return statement_completeness.group_cash_statement(lines)
    text = _statement_text(path)
    lines = statement_completeness.parse_current_account(text)
    if not lines:
        return []
    period = statement_completeness.parse_statement_period(text)
    return [(lines[0].portfolio, lines, period)]


def _run_reconcile_transactions(
    export_paths: list[Path], sidecar_dir: Path, out_dir: Path
) -> tuple[int, int, int, int]:
    """Diff each portal Transactions CSV against the sidecars by ``Order nr.``.

    Shared by the ``reconcile-transactions`` command and the ``rebuild``
    post-step. Writes one ``summary-<portfolio>-<period-end>.txt`` +
    ``findings-<...>.csv`` per mandate. Returns
    ``(total_missing, total_unmatched, total_mismatch, written)``, leaving the
    fail-or-not decision to the caller.
    """

    rows = _load_sidecar_rows(sidecar_dir)
    lettered = statement_completeness.lettered_portfolio_map(rows)
    total_missing = total_unmatched = total_mismatch = written = 0
    for path in export_paths:
        export_rows = transactions_export.parse_transactions_csv(path)
        if not export_rows:
            err_console.print(
                f"[yellow]{path.name}: no transaction rows found — skipped[/yellow]"
            )
            continue
        for portfolio, grp, period in transactions_export.group_by_portfolio(
            export_rows
        ):
            if not statement_completeness.portfolio_is_known(portfolio, lettered):
                err_console.print(
                    f"[yellow]{path.name}: portfolio {portfolio} has no ingested "
                    f"sidecars — its orders will read MISSING[/yellow]"
                )
            portfolio = statement_completeness.resolve_portfolio(portfolio, lettered)
            report = transactions_export.reconcile(
                grp, rows, portfolio=portfolio, period=period
            )
            key = f"{portfolio}-{period[1] if period is not None else path.stem}"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"summary-{key}.txt").write_text(
                transactions_export.render_summary(path.name, report),
                encoding="utf-8",
            )
            (out_dir / f"findings-{key}.csv").write_text(
                transactions_export.render_csv(path.name, report),
                encoding="utf-8",
            )
            total_missing += len(report.missing_in_ledger)
            total_unmatched += len(report.unmatched_in_ledger)
            total_mismatch += len(report.amount_mismatches)
            written += 1
    return total_missing, total_unmatched, total_mismatch, written


