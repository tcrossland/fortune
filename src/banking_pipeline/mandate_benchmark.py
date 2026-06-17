"""Mandate value-add vs benchmarks — step 3.

Compares the mandate's **gross** (unlevered) time-weighted return against
one or more passive benchmarks, to isolate what the active management
actually *added* over just holding the market. The gross basis is the like-
for-like comparison: it's the return on the asset book before leverage, so
it's measured against an unlevered index (the net/levered return would
conflate the manager's skill with the Lombard decision).

Inputs:

* the mandate's per-period market returns + their statement-date
  boundaries, from
  :func:`banking_pipeline.mandate_returns.aggregate_period_returns`;
* a CSV of benchmark **index levels** (GBP, total-return — i.e. a tracker
  fund's accumulating NAV or a TR index), ``date`` plus one column per
  benchmark. Each benchmark is sampled at the identical period boundaries
  (as-of the nearest level on or before each date) and its period return is
  ``level_end / level_start − 1``.

Value-add per benchmark is computed over the **common window** (only the
periods where that benchmark has data), so the mandate and benchmark TWRs
in a row are always apples-to-apples. It is a simple return difference, not
risk- or beta-adjusted. A reporting aid, not advice.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from banking_pipeline.mandate_returns import PeriodReturn


@dataclass(frozen=True)
class Benchmark:
    name: str
    levels: tuple[tuple[date, Decimal], ...]  # sorted ascending by date

    def level_as_of(self, on_date: date) -> Decimal | None:
        """The latest level on or before ``on_date`` (as-of forward-fill);
        ``None`` if the benchmark has no data that early."""

        dates = [d for d, _ in self.levels]
        idx = bisect_right(dates, on_date)
        return self.levels[idx - 1][1] if idx > 0 else None


@dataclass(frozen=True)
class YearValueAdd:
    year: int
    value_add: float  # mandate − benchmark, over that calendar year's periods


@dataclass(frozen=True)
class ValueAdd:
    name: str
    window_start: date | None
    window_end: date | None
    periods: int  # aligned sub-periods (benchmark had data both ends)
    mandate_twr: float | None  # gross, over the common window
    mandate_twr_pa: float | None
    benchmark_twr: float | None
    benchmark_twr_pa: float | None
    value_add_pa: float | None  # mandate − benchmark, annualised
    value_add_cumulative: float | None  # geometric, over the window
    per_year: tuple[YearValueAdd, ...]  # value-add bucketed by calendar year
    # Up/down-market capture: the mandate's chained return over the months the
    # benchmark *rose* (up) / *fell* (down), as a ratio of the benchmark's.
    # down < 100% = fell less (protection); up < 100% = captured less upside.
    up_capture: float | None
    down_capture: float | None
    up_months: int
    down_months: int


@dataclass(frozen=True)
class BenchmarkReport:
    rows: tuple[ValueAdd, ...]
    skipped: tuple[str, ...]  # benchmarks with too little overlapping data
    # The mandate's own gross return per calendar year (benchmark-independent).
    mandate_annual: tuple[tuple[int, float], ...]


# --- loading ----------------------------------------------------------------


def _parse_date(token: str) -> date | None:
    token = token.strip()
    try:
        return date.fromisoformat(token)
    except ValueError:
        pass
    # Accept YYYY-MM (month) → first of the month.
    try:
        year, month = token.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return None


def load_benchmarks(path: Path) -> list[Benchmark]:
    """Parse a benchmark-levels CSV: ``date`` + one column per benchmark.

    A blank cell is allowed (a benchmark that doesn't cover that date); rows
    with an unparseable date or a non-numeric level are skipped for that
    cell, so partial coverage is fine."""

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = [r for r in reader if r and not r[0].lstrip().startswith("#")]
    if len(rows) < 2:
        return []

    header = [h.strip() for h in rows[0]]
    names = header[1:]
    series: dict[str, list[tuple[date, Decimal]]] = {n: [] for n in names}
    for row in rows[1:]:
        on_date = _parse_date(row[0]) if row else None
        if on_date is None:
            continue
        for name, cell in zip(names, row[1:], strict=False):
            cell = cell.strip()
            if not cell:
                continue
            try:
                series[name].append((on_date, Decimal(cell.replace(",", ""))))
            except (InvalidOperation, ValueError):
                continue
    return [
        Benchmark(name=n, levels=tuple(sorted(series[n], key=lambda x: x[0])))
        for n in names
        if series[n]
    ]


# --- value-add maths --------------------------------------------------------


def _chain(returns: list[float]) -> float:
    factor = 1.0
    for r in returns:
        factor *= 1.0 + r
    return factor - 1.0


def _annualise(cumulative: float, start: date, end: date) -> float:
    years = (end - start).days / 365.25
    if years <= 0:
        return cumulative
    return float((1.0 + cumulative) ** (1.0 / years)) - 1.0


def _capture(pairs: list[tuple[float, float]]) -> float | None:
    """Mandate's chained return over a set of (mandate, benchmark) months as a
    ratio of the benchmark's. ``None`` if empty or the benchmark netted flat."""

    if not pairs:
        return None
    bench = _chain([b for _, b in pairs])
    if bench == 0:
        return None
    return _chain([m for m, _ in pairs]) / bench


def _value_add_for(periods: list[PeriodReturn], bench: Benchmark) -> ValueAdd | None:
    """Align ``bench`` to the mandate periods and compute value-add over the
    common window, plus the per-year breakdown and up/down-market capture.
    ``None`` when fewer than one period overlaps."""

    # (period, mandate_return, benchmark_return) for each aligned sub-period.
    aligned: list[tuple[PeriodReturn, float, float]] = []
    for p in periods:
        if p.gross_return is None:
            continue
        lvl_start = bench.level_as_of(p.start)
        lvl_end = bench.level_as_of(p.end)
        if lvl_start is None or lvl_end is None or lvl_start == 0:
            continue
        aligned.append((p, p.gross_return, float(lvl_end / lvl_start) - 1.0))

    if not aligned:
        return None
    window_start, window_end = aligned[0][0].start, aligned[-1][0].end

    mandate_cum = _chain([m for _, m, _ in aligned])
    bench_cum = _chain([b for _, _, b in aligned])
    mandate_pa = _annualise(mandate_cum, window_start, window_end)
    bench_pa = _annualise(bench_cum, window_start, window_end)

    # Per-calendar-year value-add (bucket by the period's start-month year).
    by_year: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p, m, b in aligned:
        by_year[p.start.year].append((m, b))
    per_year = tuple(
        YearValueAdd(year=y, value_add=_chain([m for m, _ in v]) - _chain([b for _, b in v]))
        for y, v in sorted(by_year.items())
    )

    ups = [(m, b) for _, m, b in aligned if b > 0]
    downs = [(m, b) for _, m, b in aligned if b < 0]
    return ValueAdd(
        name=bench.name,
        window_start=window_start,
        window_end=window_end,
        periods=len(aligned),
        mandate_twr=mandate_cum,
        mandate_twr_pa=mandate_pa,
        benchmark_twr=bench_cum,
        benchmark_twr_pa=bench_pa,
        value_add_pa=mandate_pa - bench_pa,
        value_add_cumulative=(1.0 + mandate_cum) / (1.0 + bench_cum) - 1.0,
        per_year=per_year,
        up_capture=_capture(ups),
        down_capture=_capture(downs),
        up_months=len(ups),
        down_months=len(downs),
    )


def build_report(
    periods: list[PeriodReturn], benchmarks: list[Benchmark]
) -> BenchmarkReport:
    rows: list[ValueAdd] = []
    skipped: list[str] = []
    for bench in benchmarks:
        va = _value_add_for(periods, bench)
        if va is None:
            skipped.append(bench.name)
        else:
            rows.append(va)

    # The mandate's own gross return per calendar year — benchmark-independent,
    # so the per-year matrix has a stable "what did the mandate do" reference.
    by_year: dict[int, list[float]] = defaultdict(list)
    for p in periods:
        if p.gross_return is not None:
            by_year[p.start.year].append(p.gross_return)
    mandate_annual = tuple((y, _chain(v)) for y, v in sorted(by_year.items()))

    return BenchmarkReport(
        rows=tuple(rows), skipped=tuple(skipped), mandate_annual=mandate_annual
    )


# --- rendering --------------------------------------------------------------


def _pctf(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f}%"


def _pct_plain(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: BenchmarkReport) -> str:
    lines = [
        "# Mandate value-add vs benchmarks",
        "",
        "The mandate's **gross** (unlevered) time-weighted return against "
        "each passive benchmark, over the window each benchmark covers. "
        "*Value-add* is the return the active management added over simply "
        "holding that benchmark — a plain return difference, **not** risk- "
        "or beta-adjusted. The gross basis is the like-for-like comparison "
        "(the net/levered return would mix in the Lombard decision). A "
        "reporting aid, not advice.",
        "",
    ]
    if not report.rows:
        lines += [
            "No benchmark data aligned to the mandate's statement dates. "
            "Provide a benchmark-levels CSV (run "
            "`scripts/fetch_benchmarks.py`, or pass `--benchmarks`).",
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    lines += [
        "| Benchmark | Window | Mandate TWR p.a. | Benchmark TWR p.a. "
        "| Value-add p.a. | Cumulative value-add |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in report.rows:
        window = f"{r.window_start} → {r.window_end}"
        lines.append(
            f"| {r.name} | {window} | {_pct_plain(r.mandate_twr_pa)} "
            f"| {_pct_plain(r.benchmark_twr_pa)} | {_pctf(r.value_add_pa)} "
            f"| {_pctf(r.value_add_cumulative)} |"
        )
    lines.append("")
    lines += [
        "> Value-add is gross of the Lombard leverage and not risk-adjusted; "
        "a benchmark's window is only the period it has levels for, so the "
        "mandate TWR in each row is recomputed over that same window. "
        "Benchmarks should be GBP total-return (an accumulating tracker NAV "
        "or a TR index).",
        "",
    ]

    # --- year by year -------------------------------------------------------
    years = [y for y, _ in report.mandate_annual]
    if years:
        lines += [
            "## Year by year",
            "",
            "The mandate's own gross return each calendar year (top row), then "
            "**value-add** (mandate − benchmark) per year for each benchmark — "
            "so you can see *when* value was added or lost, not just the "
            "blended average. A balanced mandate is expected to trail in equity "
            "rallies and lose less in drawdowns. First/last years are partial. "
            "Single years are noisy — read the path, not one cell.",
            "",
            "| Return / value-add | " + " | ".join(str(y) for y in years) + " |",
            "| --- " + "| ---: " * len(years) + "|",
            "| **Mandate return** | "
            + " | ".join(_pctf(r) for _, r in report.mandate_annual)
            + " |",
        ]
        for r in report.rows:
            ymap = {yv.year: yv.value_add for yv in r.per_year}
            cells = " | ".join(
                _pctf(ymap[y]) if y in ymap else "—" for y in years
            )
            lines.append(f"| {r.name} | {cells} |")
        lines.append("")

    # --- up/down-market capture --------------------------------------------
    lines += [
        "## Up- vs down-market capture",
        "",
        "In the months each benchmark *rose* vs *fell*, how much of its move "
        "the mandate captured (gross). **Down-capture < 100%** = the mandate "
        "fell less (downside protection — a balanced mandate's job); "
        "**up-capture < 100%** = it captured less of the rally (the cost of "
        "caution). A defensive mandate earns its keep when down-capture is "
        "well below up-capture.",
        "",
        "| Benchmark | Down-capture (months) | Up-capture (months) |",
        "| --- | ---: | ---: |",
    ]
    for r in report.rows:
        lines.append(
            f"| {r.name} | {_pct_plain(r.down_capture)} ({r.down_months}) "
            f"| {_pct_plain(r.up_capture)} ({r.up_months}) |"
        )
    lines.append("")

    if report.skipped:
        lines += [
            "Benchmarks skipped (no overlapping data): "
            + ", ".join(report.skipped) + ".",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: BenchmarkReport) -> list[list[str]]:
    rows: list[list[str]] = [[
        "benchmark", "window_start", "window_end", "periods",
        "mandate_twr", "mandate_twr_pa",
        "benchmark_twr", "benchmark_twr_pa",
        "value_add_pa", "value_add_cumulative",
        "down_capture", "up_capture", "down_months", "up_months",
    ]]

    def f(v: float | None) -> str:
        return "" if v is None else f"{v:.6f}"

    for r in report.rows:
        rows.append([
            r.name,
            r.window_start.isoformat() if r.window_start else "",
            r.window_end.isoformat() if r.window_end else "",
            str(r.periods),
            f(r.mandate_twr), f(r.mandate_twr_pa),
            f(r.benchmark_twr), f(r.benchmark_twr_pa),
            f(r.value_add_pa), f(r.value_add_cumulative),
            f(r.down_capture), f(r.up_capture),
            str(r.down_months), str(r.up_months),
        ])
    # Per-year value-add (long form) appended after a blank separator row.
    rows.append([])
    rows.append(["per_year", "benchmark", "year", "value_add"])
    for r in report.rows:
        for yv in r.per_year:
            rows.append(["", r.name, str(yv.year), f(yv.value_add)])
    return rows
