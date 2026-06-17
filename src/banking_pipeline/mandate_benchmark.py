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


@dataclass(frozen=True)
class BenchmarkReport:
    rows: tuple[ValueAdd, ...]
    skipped: tuple[str, ...]  # benchmarks with too little overlapping data


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


def _value_add_for(periods: list[PeriodReturn], bench: Benchmark) -> ValueAdd | None:
    """Align ``bench`` to the mandate periods and compute value-add over the
    common window. ``None`` when fewer than one period overlaps."""

    mandate_rets: list[float] = []
    bench_rets: list[float] = []
    window_start: date | None = None
    window_end: date | None = None
    for p in periods:
        if p.gross_return is None:
            continue
        lvl_start = bench.level_as_of(p.start)
        lvl_end = bench.level_as_of(p.end)
        if lvl_start is None or lvl_end is None or lvl_start == 0:
            continue
        mandate_rets.append(p.gross_return)
        bench_rets.append(float(lvl_end / lvl_start) - 1.0)
        window_start = window_start or p.start
        window_end = p.end

    if not mandate_rets or window_start is None or window_end is None:
        return None

    mandate_cum = _chain(mandate_rets)
    bench_cum = _chain(bench_rets)
    mandate_pa = _annualise(mandate_cum, window_start, window_end)
    bench_pa = _annualise(bench_cum, window_start, window_end)
    return ValueAdd(
        name=bench.name,
        window_start=window_start,
        window_end=window_end,
        periods=len(mandate_rets),
        mandate_twr=mandate_cum,
        mandate_twr_pa=mandate_pa,
        benchmark_twr=bench_cum,
        benchmark_twr_pa=bench_pa,
        value_add_pa=mandate_pa - bench_pa,
        value_add_cumulative=(1.0 + mandate_cum) / (1.0 + bench_cum) - 1.0,
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
    return BenchmarkReport(rows=tuple(rows), skipped=tuple(skipped))


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
        ])
    return rows
