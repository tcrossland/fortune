"""Typer CLI entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console

from banking_pipeline import beancount_writer, portfolio_aggregate, prices_extract
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.classifiers.bank import BANK_RULES, BankRuleClassifier
from banking_pipeline.classifiers.language import LANGUAGE_RULES, LanguageRuleClassifier
from banking_pipeline.classifiers.rules import DEFAULT_RULES, RuleClassifier
from banking_pipeline.extractors import extract_pages, load_pdf
from banking_pipeline.models import Classification
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Classify and extract one or more PDFs, then render beancount entries."""

    _configure_logging(verbose)
    pipeline = Pipeline()

    chunks: list[str] = []
    for path in pdf_paths:
        result = pipeline.process(path)
        chunks.append(beancount_writer.render(result))

    rendered = "\n\n".join(chunks)
    if output is None:
        console.print(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        err_console.print(f"Wrote {output}")


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
      - **Monthly-statement valuations** (opt-in via ``--statement``):
        Pictet's portfolio-valuation page lists every held ISIN's
        market price on the statement date, so a year of monthly
        statements gives ~12 price points per holding regardless of
        whether the position traded that month. This is what
        densifies the price timeline for stale holdings (a fund
        bought in 2022 and held since trades-derives only one price
        on the buy date; statements add monthly quotes from then on).
    """

    _configure_logging(verbose)
    output_path, total = prices_extract.generate(
        data_dir=data_dir,
        output=output,
        statement_files=statements,
    )
    extras = (
        f", {len(statements)} statement(s) merged" if statements else ""
    )
    err_console.print(
        f"Wrote {output_path} ({total} price directive(s){extras})"
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
