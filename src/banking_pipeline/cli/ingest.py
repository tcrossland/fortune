"""Ingestion + import commands.

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
from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.models import Transaction
from banking_pipeline.pipeline import Pipeline
from banking_pipeline.revolut import import_csvs as revolut_import_csvs
from banking_pipeline.revolut import render as revolut_render
from banking_pipeline.revolut.render import render_open_directives as revolut_open_directives
from banking_pipeline.transaction_sidecar import (
    dump_transactions,
    load_transactions,
    sidecar_path,
    transactions_to_jsonl,
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
