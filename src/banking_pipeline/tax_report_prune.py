"""Retention policy for the archived Pictet IRPF P&L tax reports.

Pictet issues a Realised and an Unrealised P&L report on most booking-event
days, so a year's ``<year>/tax/`` folder accumulates ~490 files each. These
are an archive-only reference source (never ingested; see
:mod:`banking_pipeline.archive`), so keeping every daily cut is noise. This
module holds the *pure* selection policy — no I/O — that the
``prune-tax-reports`` command wraps: given the dated report set it returns
the principled subset to keep, and the command moves the rest into a
``_superseded/`` sibling (a move, never a delete).

Policy (grouped by report kind + **calendar** year — the Spanish IRPF year,
over which the realised report runs ``01.01 → as-of``):

* **Realised** (cumulative within the year): keep the **latest as-of per
  month** (each is a restatement checkpoint) plus the **year's final**
  report. ≤ ~12/yr.
* **Unrealised** (point-in-time snapshot): keep the **latest as-of per
  month** plus the snapshot **on-or-before 5 April** (the UK tax-year-end
  anchor; the December month-end already covers 31 Dec / calendar-year-end).
  ≤ ~13/yr.

"Latest-per-month" resolves to the last booking-day report of each month —
no exact month-end is required, since reports exist only on activity days.
Net effect: ~490/yr → ~25/yr.

The parser keys on the canonical archived name the filing stage writes,
``<Realised|Unrealised> PL <YYYYMMDD>.pdf`` — so only normalised files
participate; ``ETE`` / ``Modelo 720`` and any unrecognised name are ignored.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

TaxReportKind = Literal["Realised", "Unrealised"]

# Where pruned / superseded reports are moved (a sibling of the ``tax/``
# folder's contents). Never descended into, so re-runs are no-ops.
SUPERSEDED_DIRNAME = "_superseded"

# The canonical filename of a *prunable* P&L report: ``Realised PL
# 20230720.pdf`` / ``Unrealised PL 20230720.pdf`` (the stem
# :func:`archive.destination_for` writes). Anchored so nothing else matches.
# Only these participate in the retention policy — the annual fiscal
# statement (below) is kept, not pruned.
_NAME = re.compile(r"^(Realised|Unrealised) PL (\d{4})(\d{2})(\d{2})\.pdf$")

# Every canonical tax-report filename, including the annual ``Fiscal
# statement <YYYYMMDD>.pdf`` (which is filed but never pruned). Used to tell
# an already-normalised report from a legacy-named stray — so the sweep
# doesn't classify a filed statement, resolve it back to its own path, and
# move it aside as a "duplicate".
_CANONICAL_NAME = re.compile(
    r"^(?:Realised PL|Unrealised PL|Fiscal statement) \d{8}\.pdf$"
)


def is_canonical_name(name: str) -> bool:
    """True if ``name`` is a canonical tax-report filename (a P&L report or
    the annual ``Fiscal statement``).

    Used to tell an already-normalised report from a legacy-named one (which
    must be deduped by *content*, not its filename — the legacy names encode
    the download date, not the report's as-of)."""

    return _CANONICAL_NAME.match(name) is not None


@dataclass(frozen=True)
class TaxReport:
    """One dated, canonically-named P&L report in the archive."""

    kind: TaxReportKind
    as_of: date
    path: Path


def parse_tax_report(path: Path) -> TaxReport | None:
    """A :class:`TaxReport` for ``path`` if its name is a canonical P&L
    report, else ``None`` (ETE / Modelo 720 / any non-conforming name).

    A day/month that doesn't form a real date (shouldn't occur for a filed
    report) yields ``None`` rather than raising."""

    match = _NAME.match(path.name)
    if match is None:
        return None
    kind, year, month, day = match.groups()
    try:
        as_of = date(int(year), int(month), int(day))
    except ValueError:
        return None
    # ``kind`` is constrained to the two literals by the regex alternation.
    return TaxReport(kind=kind, as_of=as_of, path=path)  # type: ignore[arg-type]


def select_retained(reports: Iterable[TaxReport]) -> set[Path]:
    """The subset of ``reports`` to keep under the retention policy.

    Pure: depends only on the ``(kind, as_of)`` of each report. Everything
    not returned is a prune candidate (→ ``_superseded/``). See the module
    docstring for the policy.
    """

    by_group: dict[tuple[TaxReportKind, int], list[TaxReport]] = defaultdict(list)
    for report in reports:
        by_group[(report.kind, report.as_of.year)].append(report)

    keep: set[Path] = set()
    for (kind, year), group in by_group.items():
        # Latest report per calendar month (last activity day of the month).
        # ``max`` over (as_of, path) makes the pick deterministic if two
        # reports somehow share an as-of date.
        latest_per_month: dict[int, TaxReport] = {}
        for report in group:
            month = report.as_of.month
            current = latest_per_month.get(month)
            if current is None or (report.as_of, report.path) > (
                current.as_of,
                current.path,
            ):
                latest_per_month[month] = report
        keep.update(r.path for r in latest_per_month.values())

        if kind == "Realised":
            # The year's final report (restatement of record). Already the
            # latest month's latest, but union it explicitly for clarity.
            year_final = max(group, key=lambda r: (r.as_of, r.path))
            keep.add(year_final.path)
        else:
            # The latest snapshot on-or-before 5 April — the UK tax-year-end
            # anchor, which a plain month-end pick would miss when an
            # early-April cut precedes a later-April one.
            cutoff = date(year, 4, 5)
            eligible = [r for r in group if r.as_of <= cutoff]
            if eligible:
                anchor = max(eligible, key=lambda r: (r.as_of, r.path))
                keep.add(anchor.path)

    return keep


@dataclass(frozen=True)
class PrunePlan:
    """The keep / move split for one ``<year>/tax/`` folder."""

    year: int
    keep: list[TaxReport]
    supersede: list[TaxReport]


def plan_prune(reports: Iterable[TaxReport]) -> list[PrunePlan]:
    """Group ``reports`` by year and split each into keep / supersede lists,
    sorted for stable display (by kind then as-of within each list)."""

    retained = select_retained(reports)
    by_year: dict[int, list[TaxReport]] = defaultdict(list)
    for report in reports:
        by_year[report.as_of.year].append(report)

    plans: list[PrunePlan] = []
    for year in sorted(by_year):
        group = by_year[year]
        key = lambda r: (r.kind, r.as_of, r.path)  # noqa: E731
        keep = sorted((r for r in group if r.path in retained), key=key)
        supersede = sorted((r for r in group if r.path not in retained), key=key)
        plans.append(PrunePlan(year=year, keep=keep, supersede=supersede))
    return plans


def discover_reports(tax_dir: Path) -> list[TaxReport]:
    """Every canonically-named P&L report directly in ``tax_dir``.

    Only the folder's top level is scanned — the ``_superseded/`` sibling is
    never descended into, so a re-run is a no-op. Non-conforming names (ETE,
    Modelo 720, legacy variants not yet normalised) are skipped.
    """

    reports: list[TaxReport] = []
    if not tax_dir.is_dir():
        return reports
    for path in sorted(tax_dir.glob("*.pdf")):
        report = parse_tax_report(path)
        if report is not None:
            reports.append(report)
    return reports
