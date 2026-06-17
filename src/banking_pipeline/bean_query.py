"""Wrapper around the ``bean-query`` CLI binary.

We shell out to ``bean-query`` for the one report that genuinely needs
beancount's loader — the trial balance, whose per-account balances depend
on cost-basis booking (the elastic ``Realized`` / ``Unrealized`` legs are
computed at load time). This mirrors :mod:`banking_pipeline.bean_check`:
``beancount`` is GPL-2.0, so we invoke its binaries as separate processes
rather than importing it. (The statement-valuation reports —
``concentration`` / ``net-worth`` / … — deliberately avoid the ledger and
read statement marks instead; the trial balance is the exception because
it *is* a ledger construct.)
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class QueryResult:
    """Outcome of one ``bean-query`` invocation.

    ``rows`` is the parsed CSV body (header dropped); empty on any failure.
    ``binary_missing`` is set when ``bean-query`` isn't on ``PATH`` — the
    caller branches on it to print an install hint instead of a misleading
    error, exactly as :class:`banking_pipeline.bean_check.CheckResult` does.
    ``error`` carries the binary's stderr on a non-zero exit.
    """

    rows: list[list[str]] = field(default_factory=list)
    returncode: int = 0
    error: str = ""
    binary_missing: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.binary_missing


def find_bean_query() -> Path | None:
    """Path to the ``bean-query`` binary, or ``None`` if not on ``PATH``."""

    found = shutil.which("bean-query")
    return Path(found) if found else None


def run_query(ledger: Path, bql: str) -> QueryResult:
    """Run ``bql`` against ``ledger`` via ``bean-query -f csv`` and parse it.

    Returns a :class:`QueryResult` rather than raising. A missing binary
    yields ``binary_missing=True`` (the caller treats it as "validation
    opted out", like the check step); a non-zero exit yields the captured
    stderr in ``error``.
    """

    binary = find_bean_query()
    if binary is None:
        return QueryResult(
            error=(
                "bean-query binary not found on PATH; trial balance skipped. "
                "Install with `uv tool install beancount` (the GPL-2.0 "
                "licence applies to bean-query itself, not to this codebase, "
                "since we shell out rather than link)."
            ),
            binary_missing=True,
        )

    proc = subprocess.run(
        [str(binary), "-f", "csv", str(ledger), bql],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return QueryResult(
            returncode=proc.returncode, error=(proc.stderr or proc.stdout or "")
        )
    rows = list(csv.reader(io.StringIO(proc.stdout)))
    return QueryResult(rows=rows[1:] if rows else [])  # drop the header row
