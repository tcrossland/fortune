"""Reusable Typer option aliases shared across CLI commands.

Several commands take the same options with the same meaning — most
visibly ``--verbose`` (on every command) and the statement-valuation
option set shared by ``concentration`` / ``net-worth`` / ``allocation`` /
``portfolio-allocation``. Declaring each inline duplicated the help text
many times and let the wording drift apart. These ``Annotated`` aliases
are the single definition; a command annotates a parameter with the alias
and supplies only the default (Typer's list-option default still lives at
the call site).

Options whose help is genuinely command-specific — ``--out`` (names a
different ``*_reports_dir`` per command) and ``--strict`` (means a
different thing per command) — are deliberately left inline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v")]

# --- statement-valuation report option set ---------------------------------
# Shared by concentration / net-worth / allocation / portfolio-allocation.

StatementOpt = Annotated[
    list[Path],
    typer.Option(
        "--statement",
        help="Statement PDF (or pre-extracted ``.txt`` dump) — a Pictet "
        "monthly statement or a Vanguard ISA regular statement. Repeat the "
        "flag to pass several (a whole history is fine; the latest per "
        "portfolio wins where a single snapshot is needed).",
    ),
]

StatementsDirOpt = Annotated[
    Path | None,
    typer.Option(
        "--statements-dir",
        help="Directory to scan for valuation-bearing statements (same "
        "classifier filter as ``prices``). Point it at the whole statement "
        "archive to get the full history.",
    ),
]

StatementsRecursiveOpt = Annotated[
    bool,
    typer.Option(
        "--statements-recursive",
        "-R",
        help="Descend into subdirectories under ``--statements-dir``.",
    ),
]

StatementsGlobOpt = Annotated[
    str | None,
    typer.Option(
        "--statements-glob",
        help="Filename glob applied to ``--statements-dir`` *before* "
        "classification, e.g. ``*monthly*.pdf`` (the Pictet monthly-statement "
        "naming convention the rebuild uses). This is the fast path: it prunes "
        "the walk to matching filenames so only those PDFs are opened and "
        "classified, instead of text-extracting every PDF in the tree. "
        "Defaults to ``*.pdf`` (classify everything — robust but slow on the "
        "full archive).",
    ),
]

ValuationRateSourceOpt = Annotated[
    str | None,
    typer.Option(
        "--rate-source",
        help="GBP rate source for non-GBP valuations (``null`` | "
        "``hmrc-monthly``). Defaults to the configured source; holdings "
        "with no rate are excluded and flagged.",
    ),
]

CommoditiesOpt = Annotated[
    Path | None,
    typer.Option(
        "--commodities",
        help="Commodity-metadata TOML (asset class / domicile / tax flags). "
        "Defaults to the configured ``commodities_metadata_path``.",
    ),
]

PropertyOpt = Annotated[
    Path | None,
    typer.Option(
        "--property",
        help="Property TOML to fold off-ledger residential property into the "
        "report. Defaults to the configured ``property_path``.",
    ),
]
