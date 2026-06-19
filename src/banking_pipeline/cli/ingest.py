"""Ingestion + import commands.

``import`` (the first pipeline stage: file raw PDFs into a dated tree),
``ingest`` (classify/extract/render PDFs to beancount), ``dump-transactions``
(print the JSONL sidecar), ``dedup-check`` (duplicate audit), and ``revolut``
(the CSV side path). Shared helpers (logging, bean-check) come from
:mod:`banking_pipeline.cli._main`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from banking_pipeline import (
    archive,
    beancount_writer,
    dedup,
)
from banking_pipeline.cli._main import (
    _configure_logging,
    _run_check_or_exit,
    app,
    console,
    err_console,
)
from banking_pipeline.cli_options import (
    VerboseOpt,
)
from banking_pipeline.config import settings
from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.models import ExtractionResult, Transaction
from banking_pipeline.pipeline import Pipeline
from banking_pipeline.revolut import import_csvs as revolut_import_csvs
from banking_pipeline.revolut import render as revolut_render
from banking_pipeline.revolut.render import render_open_directives as revolut_open_directives
from banking_pipeline.switch_pairing import pair_switches
from banking_pipeline.transaction_sidecar import (
    dump_transactions,
    load_transactions,
    sidecar_path,
    transactions_to_jsonl,
)
from banking_pipeline.writer.builders.switch_trade import SWITCH_TYPES


def _apply_switch_pairing(txns: list[Transaction], *, strict: bool) -> None:
    """Pair switch legs in ``txns`` and stamp the shared ``link_id`` in place.

    Runs the pure :func:`pair_switches` matcher, applies each assignment to
    the in-memory ``Transaction`` (so both the rendered ledger and the
    sidecar carry the shared link by construction), warns on every unpaired
    leg, and — under ``strict`` — fails when an in-batch orphan (an
    opposite-side counterpart that should have paired but didn't) is found.
    """

    pairing = pair_switches(txns)
    for tx in txns:
        # Apply only to switch legs: assignments are keyed by
        # ``transaction_number``, and though Pictet numbers are per-advice
        # unique, guarding on the doctype means a non-switch advice that
        # ever collided on a number can't inherit a switch's link.
        if tx.document_type not in SWITCH_TYPES:
            continue
        link = pairing.assignments.get(tx.transaction_number or "")
        if link is not None:
            tx.link_id = link

    for tx in pairing.unpaired:
        when = tx.booking_date or tx.trade_date
        err_console.print(
            f"[yellow]warning:[/yellow] unpaired switch leg "
            f"{tx.transaction_number} "
            f"({tx.currency} {tx.amount}, booked {when}) — kept its own link"
        )

    if strict and pairing.in_batch_orphans:
        numbers = ", ".join(
            str(tx.transaction_number) for tx in pairing.in_batch_orphans
        )
        err_console.print(
            f"[red]error:[/red] switch legs share a batch but didn't pair "
            f"(likely an extraction bug): {numbers}"
        )
        raise typer.Exit(code=1)


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
    verbose: VerboseOpt = False,
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

    # Collect every result first, then pair switch legs across the whole
    # batch, then render — a switch's salida↔entrada link can't be known
    # until both legs are in hand, so rendering can't happen inside the
    # per-document loop (see ``switch_pairing``).
    results: list[ExtractionResult] = []
    all_txns: list[Transaction] = []
    for path in pdf_paths:
        try:
            result = pipeline.process(path)
        except TemplateExtractionError as exc:
            err_console.print(
                f"[red]extraction error:[/red] {exc}"
            )
            raise typer.Exit(code=1) from exc
        results.append(result)
        all_txns.extend(result.transactions)

    _apply_switch_pairing(all_txns, strict=strict)

    rendered = "\n\n".join(beancount_writer.render(result) for result in results)

    # No ``close`` directives here. ingest output is a partial slice of
    # history that the portfolio aggregate ``include``s; a per-batch close
    # can't see a later source re-acquiring the holding, and beancount
    # can't reopen a closed account. Close emission is aggregate-aware
    # (see ``portfolio_aggregate``), which sees the full history.

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
    verbose: VerboseOpt = False,
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
    verbose: VerboseOpt = False,
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


@app.command("import")
def import_documents(
    source: Annotated[
        Path | None,
        typer.Argument(
            help="Folder of incoming PDFs (top level only) or a .zip of them "
            "(the bank's bulk-download shape). Defaults to the "
            "``import_source_dir`` setting.",
        ),
    ] = None,
    dest: Annotated[
        Path | None,
        typer.Argument(
            help="Archive root to file into, as "
            "``<root>/<year>/<account>/<YYYYMMDD>-<reference>.pdf``. "
            "Defaults to the ``import_archive_dir`` setting.",
        ),
    ] = None,
    pattern: Annotated[
        str,
        typer.Option(
            "--pattern",
            help="Glob for files to file. Case-insensitive on the extension; "
            "the default picks up both .pdf and .PDF.",
        ),
    ] = "*.pdf",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Print the planned moves without touching any file.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """File raw bank PDFs into a dated ``<year>/<account>/`` archive tree.

    The first stage of the pipeline: it organises a folder (or a .zip, the
    shape the bank's bulk download arrives in) of fresh downloads so the
    later ingest / report stages read from a stable tree.
    Bank and document type come from the shared classifier; the account
    number, per-document reference and publication date are scraped to build
    ``<dest>/<year>/<account>/<YYYYMMDD>-<reference>.pdf``. Two documents that
    share a reference within the batch (e.g. an invoice and its debit-of-fees
    advice) are filed with a doctype suffix so neither clobbers the other. A
    file whose destination already exists is left in place (never
    overwritten); a PDF the classifier can't place is reported as unmatched
    and skipped. Pictet documents (both locales) are recognised today.
    ``source`` / ``dest`` fall back to the ``import_source_dir`` /
    ``import_archive_dir`` settings when omitted.
    """

    _configure_logging(verbose)

    dest = dest or settings.import_archive_dir

    # Resolve the import source(s): an explicit argument wins; else the
    # configured glob (typically the bank's periodic zips, filed as one
    # batch); else the configured single directory / zip.
    from_glob = False
    sources: list[Path] | None
    if source is not None:
        sources = [source]
    elif settings.import_source_glob:
        from_glob = True
        sources = archive.expand_source_glob(settings.import_source_glob)
        if not sources:
            err_console.print(
                f"[yellow]warning:[/yellow] no files matched "
                f"{settings.import_source_glob}"
            )
    elif settings.import_source_dir is not None:
        sources = [settings.import_source_dir]
    else:
        sources = None

    if sources is None or dest is None:
        err_console.print(
            "[red]error:[/red] an import source and archive are required — "
            "pass them as arguments or set import_source_glob / "
            "import_source_dir and import_archive_dir."
        )
        raise typer.Exit(code=2)

    # A user-named single source (argument or import_source_dir) must be an
    # existing directory or .zip — catch typos early. Glob matches are valid
    # by construction (and may include loose PDFs).
    if not from_glob:
        named = sources[0]
        is_zip = named.is_file() and named.suffix.lower() == ".zip"
        if not named.is_dir() and not is_zip:
            err_console.print(
                f"[red]error:[/red] source {named} must be an existing "
                "directory or a .zip file"
            )
            raise typer.Exit(code=2)

    # ``source_pdfs`` resolves each source (directory glob, .zip extraction,
    # or loose PDF) and keeps every zip extraction alive for the one block.
    with archive.source_pdfs(sources, pattern) as pdfs:
        plans = archive.file_documents(pdfs, dest, dry_run=dry_run)

    # Show the source as its bare filename: the source root is already known
    # (the user passed it), and for a .zip it's the temp-extraction path
    # whose basename is the original zip entry. The destination stays a full
    # archive path so the year/account placement is visible.
    moved = skipped = unmatched = errored = 0
    for plan in plans:
        if plan.status == "move":
            moved += 1
            verb = "would file" if dry_run else "filed"
            line = f"{verb}: {plan.source.name} -> {plan.destination}"
        elif plan.status == "skip":
            skipped += 1
            line = f"skip (exists): {plan.source.name} -> {plan.destination}"
        elif plan.status == "no-match":
            unmatched += 1
            line = f"no match: {plan.source.name}"
        else:  # error
            errored += 1
            line = f"error: {plan.source.name}: {plan.detail}"
        console.print(line, markup=False, highlight=False, soft_wrap=True)

    prefix = "[dry-run] " if dry_run else ""
    err_console.print(
        f"{prefix}{moved} {'to file' if dry_run else 'filed'}, "
        f"{skipped} skipped, {unmatched} unmatched, {errored} error(s).",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


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
    verbose: VerboseOpt = False,
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
