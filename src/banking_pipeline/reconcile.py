"""Statement-balance reconciliation.

Compares the **statement-asserted** balances (the ``balance`` directives
in ``data/balances.beancount``, sourced from each Pictet monthly
statement's portfolio-valuation page) against the **ledger-computed**
balances (what the ingested transactions actually sum to).

This is additive to ``bean-check``, not a replacement — and it is built
*on top of* ``bean-check`` rather than re-deriving balances. ``bean-check``
already evaluates every assertion in one pass and prints a
``Balance failed for '<account>': expected X != accumulated Y`` line for
each one that drifts. Reconcile parses those lines and turns them into a
report ``bean-check`` doesn't give on its own:

* every drifted assertion with expected / actual / signed difference,
* the *earliest* date each account diverged (localises a missed or
  misclassified document to one statement month), and
* coverage gaps — statement months with no assertion at all, which a
  missing checkpoint can't surface through ``bean-check``.

Delegating the verdict to ``bean-check`` means reconcile agrees with a
load *by construction*: an assertion is "drift" iff ``bean-check``
flagged it, so beancount's inferred-from-decimals tolerance is honoured
without re-implementing it. We never ``import beancount`` — the binary
is invoked via :mod:`banking_pipeline.bean_check` (the GPL-2.0
shell-out rationale in the README).

The module is deliberately pure: the ``bean-check`` output is passed in
as text (the CLI sources it), so the parse / diff / coverage logic is
unit-testable without the binary.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

# ``<date> balance <account>  <quantity> <commodity>`` — the shape
# emitted by ``balances_extract.render``. Leading whitespace tolerated;
# comment (``;``) and blank lines simply don't match.
_ASSERTION_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+balance\s+(\S+)\s+(-?[\d.]+)\s+(\S+)\s*$"
)

# A single ``bean-check`` balance-assertion failure line, e.g.::
#
#   data/balances.beancount:42: Balance failed for 'Assets:Pic:K1:GBP':
#       expected 999.00 GBP != accumulated 500.00 GBP (499.00 too little)
#
# The ``<file>:<line>:`` prefix cites the directive's own source line,
# which lets us match a failure back to the exact assertion it came from
# (robust against an account asserting the same value on two dates).
_FAILURE_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\s*Balance failed for "
    r"'(?P<account>[^']+)':\s*expected\s+-?[\d.]+\s+\S+\s+!=\s+"
    r"accumulated\s+(?P<actual>-?[\d.]+)\s+\S+"
)


class Status(StrEnum):
    """Per-assertion reconciliation outcome."""

    OK = "ok"
    DRIFT = "drift"


@dataclass(frozen=True)
class Assertion:
    """One statement-asserted balance: the expected side of a comparison.

    ``line`` is the 1-based line number in the balances file, used to
    match a ``bean-check`` failure back to this assertion unambiguously.
    """

    date: str
    account: str
    quantity: Decimal
    commodity: str
    line: int


@dataclass(frozen=True)
class Failure:
    """A parsed ``bean-check`` balance-assertion failure."""

    line: int
    account: str
    actual: Decimal


@dataclass(frozen=True)
class ReconRow:
    """A single reconciled assertion: expected vs. actual with verdict.

    ``actual`` is ``None`` for an OK row — ``bean-check`` only prints the
    accumulated balance for assertions that *failed*, so a passing one
    has no reported actual (it reconciled within tolerance, which is all
    we need to know)."""

    date: str
    account: str
    commodity: str
    expected: Decimal
    actual: Decimal | None
    status: Status

    @property
    def diff(self) -> Decimal | None:
        """Signed ``actual - expected``. Negative = ledger short of the
        statement (likely a missed inflow / uningested document)."""

        if self.actual is None:
            return None
        return self.actual - self.expected


def parse_assertions(text: str) -> list[Assertion]:
    """Parse ``balance`` directives from a ``balances.beancount`` body.

    Records each directive's 1-based line number so a ``bean-check``
    failure can be matched back to it. Lines that don't match the
    directive shape (comments, blanks, headers) are skipped. The file
    format is our own — written by :func:`balances_extract.render` — so a
    regex parse is safe and keeps a single source of truth without
    re-extracting from statements.
    """

    assertions: list[Assertion] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _ASSERTION_RE.match(line)
        if m is None:
            continue
        assertion_date, account, qty_str, commodity = m.groups()
        try:
            quantity = Decimal(qty_str)
        except InvalidOperation:
            continue
        assertions.append(
            Assertion(assertion_date, account, quantity, commodity, lineno)
        )
    return assertions


def parse_bean_check_failures(
    text: str, balances_name: str | None = None
) -> dict[int, Failure]:
    """Parse ``bean-check`` output into a ``{line: Failure}`` map.

    ``balances_name`` (a file *basename*) optionally restricts matches to
    failures whose cited source file is the balances file — guarding
    against the rare case where another included file shares a line
    number. Non-failure lines (other diagnostics, the echoed directive)
    don't match and are ignored.
    """

    failures: dict[int, Failure] = {}
    for line in text.splitlines():
        m = _FAILURE_RE.match(line)
        if m is None:
            continue
        if balances_name is not None and Path(m.group("path")).name != balances_name:
            continue
        try:
            actual = Decimal(m.group("actual"))
        except InvalidOperation:
            continue
        lineno = int(m.group("line"))
        failures[lineno] = Failure(lineno, m.group("account"), actual)
    return failures


def reconcile(
    expected: list[Assertion], failures: dict[int, Failure]
) -> list[ReconRow]:
    """Diff each assertion against the ``bean-check`` failures.

    An assertion whose line appears in ``failures`` (and whose account
    matches, as a sanity guard) is drift, carrying the failure's
    accumulated balance as the actual. Every other assertion passed
    ``bean-check`` within tolerance and is OK with no reported actual.
    """

    rows: list[ReconRow] = []
    for a in expected:
        failure = failures.get(a.line)
        if failure is not None and failure.account == a.account:
            rows.append(
                ReconRow(
                    date=a.date,
                    account=a.account,
                    commodity=a.commodity,
                    expected=a.quantity,
                    actual=failure.actual,
                    status=Status.DRIFT,
                )
            )
        else:
            rows.append(
                ReconRow(
                    date=a.date,
                    account=a.account,
                    commodity=a.commodity,
                    expected=a.quantity,
                    actual=None,
                    status=Status.OK,
                )
            )
    return rows


def _portfolio_of(account: str) -> str:
    """Portfolio segment of a beancount account path.

    ``Assets:Pic:K123456001:GBP`` → ``K123456001`` (the segment after
    the bank prefix). Falls back to the whole account when the path is
    shorter than expected.
    """

    parts = account.split(":")
    return parts[2] if len(parts) >= 3 else account


def _statement_month(assertion_date: str) -> str:
    """The statement month an assertion covers.

    Assertions are dated one day after the statement's ``As at`` anchor
    (beancount's beginning-of-day convention), so the statement month is
    the day before: ``2026-01-01`` → ``2025-12``.
    """

    d = date.fromisoformat(assertion_date) - timedelta(days=1)
    return f"{d.year:04d}-{d.month:02d}"


def _month_range(first: str, last: str) -> list[str]:
    """Inclusive list of ``YYYY-MM`` months from ``first`` to ``last``."""

    fy, fm = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    months: list[str] = []
    y, m = fy, fm
    while (y, m) <= (ly, lm):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def find_coverage_gaps(assertions: list[Assertion]) -> dict[str, list[str]]:
    """Per-portfolio statement months missing from the asserted set.

    Statements arrive monthly, so any month between a portfolio's first
    and last asserted month that has no assertion is a likely missed
    statement. Returns ``{portfolio: [missing YYYY-MM, ...]}`` for
    portfolios with at least one gap; portfolios with full coverage are
    omitted.
    """

    months_by_portfolio: dict[str, set[str]] = {}
    for a in assertions:
        portfolio = _portfolio_of(a.account)
        months_by_portfolio.setdefault(portfolio, set()).add(
            _statement_month(a.date)
        )

    gaps: dict[str, list[str]] = {}
    for portfolio, months in months_by_portfolio.items():
        if len(months) < 2:
            continue
        expected_months = _month_range(min(months), max(months))
        missing = [m for m in expected_months if m not in months]
        if missing:
            gaps[portfolio] = missing
    return gaps


def earliest_drift(rows: list[ReconRow]) -> dict[str, str]:
    """Account → earliest drifted assertion date.

    Localises each divergence to the first statement month it appears,
    which is where the missing/misclassified document lives.
    """

    earliest: dict[str, str] = {}
    for row in rows:
        if row.status is not Status.DRIFT:
            continue
        if row.account not in earliest or row.date < earliest[row.account]:
            earliest[row.account] = row.date
    return earliest


@dataclass(frozen=True)
class ReconReport:
    """Full reconciliation outcome: rows + derived summaries."""

    rows: list[ReconRow]
    coverage_gaps: dict[str, list[str]]
    earliest_drift: dict[str, str]

    @property
    def drift_rows(self) -> list[ReconRow]:
        return [r for r in self.rows if r.status is Status.DRIFT]

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.rows if r.status is Status.OK)

    @property
    def has_drift(self) -> bool:
        return bool(self.drift_rows)


def build_report(
    expected: list[Assertion], failures: dict[int, Failure]
) -> ReconReport:
    """Reconcile and bundle the derived summaries into a report."""

    rows = reconcile(expected, failures)
    return ReconReport(
        rows=rows,
        coverage_gaps=find_coverage_gaps(expected),
        earliest_drift=earliest_drift(rows),
    )


def _fmt(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,}"


def render_summary(report: ReconReport, *, ledger: str, balances: str) -> str:
    """Human-readable ``summary.txt`` body."""

    lines = [
        f"Reconciliation — {ledger} vs {balances}",
        "",
    ]
    total = len(report.rows)

    if report.drift_rows:
        lines.append(
            f"DRIFT ({len(report.drift_rows)} of {total} assertions "
            "outside tolerance)"
        )
        lines.append(
            f"  {'date':<11} {'account':<40} "
            f"{'expected':>16} {'actual':>16} {'diff':>16}"
        )
        for r in sorted(report.drift_rows, key=lambda x: (x.date, x.account)):
            lines.append(
                f"  {r.date:<11} {r.account:<40} "
                f"{_fmt(r.expected):>16} {_fmt(r.actual):>16} "
                f"{_fmt(r.diff):>16}"
            )
        lines.append("")

    if report.earliest_drift:
        lines.append("EARLIEST DRIFT")
        for account, when in sorted(report.earliest_drift.items()):
            month = _statement_month(when)
            lines.append(
                f"  {account} first diverged {when} "
                f"→ check {month} documents"
            )
        lines.append("")

    if report.coverage_gaps:
        lines.append("COVERAGE GAPS")
        for portfolio, missing in sorted(report.coverage_gaps.items()):
            lines.append(
                f"  {portfolio}: no statement for {', '.join(missing)}"
            )
        lines.append("")

    lines.append(f"OK: {report.ok_count} of {total} assertions within tolerance")
    lines.append("")
    return "\n".join(lines)


def render_csv(report: ReconReport) -> str:
    """Machine-readable ``drift.csv`` — every reconciled row."""

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["date", "account", "commodity", "expected", "actual", "diff", "status"]
    )
    for r in sorted(report.rows, key=lambda x: (x.date, x.account)):
        writer.writerow(
            [
                r.date,
                r.account,
                r.commodity,
                str(r.expected),
                "" if r.actual is None else str(r.actual),
                "" if r.diff is None else str(r.diff),
                r.status.value,
            ]
        )
    return out.getvalue()
