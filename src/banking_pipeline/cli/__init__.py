"""Typer CLI entrypoint package.

The command implementations are being split out of the historical
monolith into sibling modules grouped by domain. This package root
assembles the Typer ``app`` and re-exports the names external callers and
tests rely on, so ``banking_pipeline.cli:app`` (the console-script entry
point) and references like ``cli.settings`` keep working unchanged as the
split progresses.
"""

from __future__ import annotations

# Importing the command submodules registers their ``@app.command``s on the
# shared ``app`` (defined in ``_main``). Side-effect import only.
from banking_pipeline.cli import reports  # noqa: E402,F401
from banking_pipeline.cli._main import (
    _write_tax_summary,
    app,
    console,
    err_console,
)
from banking_pipeline.config import settings
from banking_pipeline.extractors import load_pdf

__all__ = [
    "app",
    "console",
    "err_console",
    "load_pdf",
    "settings",
    "_write_tax_summary",
]
