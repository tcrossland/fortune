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

import sys
from collections.abc import Iterable
from pathlib import Path

import structlog
import typer
from rich.console import Console

from banking_pipeline import (
    bean_check,
    prices_extract,
)
from banking_pipeline.batch_config import (
    Source,
)
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.commodities_metadata import CommodityMetadata, load_commodities
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


