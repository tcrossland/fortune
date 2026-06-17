"""Mandate value-add vs benchmarks (step 3): CSV loading, as-of alignment,
value-add maths, rendering. The mandate periods are passed in directly
(``PeriodReturn``), so no statement parsing is exercised here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline import mandate_benchmark as mb
from banking_pipeline.mandate_returns import PeriodReturn


def _bench(name: str, points: list[tuple[date, str]]) -> mb.Benchmark:
    return mb.Benchmark(
        name=name, levels=tuple((d, Decimal(v)) for d, v in points)
    )


# --- loading --------------------------------------------------------------


def test_load_benchmarks_parses_columns_dates_and_blanks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    csv_path = tmp_path / "b.csv"
    csv_path.write_text(
        "# a comment\n"
        "date,Blend,Equity\n"
        "2021-08,100,100\n"
        "2021-09-01,101,\n"   # Equity blank → only Blend gets this point
        "bad-date,9,9\n"      # unparseable date → row skipped
        "2021-10,102,104\n",
        encoding="utf-8",
    )
    benches = {b.name: b for b in mb.load_benchmarks(csv_path)}
    assert set(benches) == {"Blend", "Equity"}
    assert len(benches["Blend"].levels) == 3
    assert len(benches["Equity"].levels) == 2  # the blank cell dropped
    # YYYY-MM parsed to the first of the month.
    assert benches["Blend"].levels[0][0] == date(2021, 8, 1)


def test_level_as_of_forward_fill() -> None:
    b = _bench("X", [(date(2021, 8, 1), "100"), (date(2021, 10, 1), "110")])
    assert b.level_as_of(date(2021, 7, 1)) is None       # before first
    assert b.level_as_of(date(2021, 8, 1)) == Decimal("100")
    assert b.level_as_of(date(2021, 9, 15)) == Decimal("100")  # as-of Aug
    assert b.level_as_of(date(2021, 11, 1)) == Decimal("110")  # as-of Oct


# --- value-add maths ------------------------------------------------------


def _periods(*rets: float) -> list[PeriodReturn]:
    # One period per month starting 2021-08-01.
    out: list[PeriodReturn] = []
    months = [date(2021, 8 + i, 1) for i in range(len(rets) + 1)]
    for i, r in enumerate(rets):
        out.append(PeriodReturn(months[i], months[i + 1], r, r))
    return out


def test_value_add_outperformance() -> None:
    # Mandate +10% then +10% (gross). Benchmark levels 100→105→110 → +5%,
    # +4.76%. Mandate beats it both periods → positive value-add.
    periods = _periods(0.10, 0.10)
    bench = _bench("B", [
        (date(2021, 8, 1), "100"),
        (date(2021, 9, 1), "105"),
        (date(2021, 10, 1), "110"),
    ])
    report = mb.build_report(periods, [bench])
    assert len(report.rows) == 1
    r = report.rows[0]
    assert r.periods == 2
    assert r.window_start == date(2021, 8, 1)
    assert r.window_end == date(2021, 10, 1)
    # Mandate cum = 1.1*1.1-1 = 0.21; benchmark cum = 110/100-1 = 0.10.
    assert r.mandate_twr is not None and abs(r.mandate_twr - 0.21) < 1e-9
    assert r.benchmark_twr is not None and abs(r.benchmark_twr - 0.10) < 1e-9
    # Geometric value-add = 1.21/1.10 - 1 = 0.10.
    assert r.value_add_cumulative is not None
    assert abs(r.value_add_cumulative - 0.10) < 1e-9
    assert r.value_add_pa is not None and r.value_add_pa > 0


def test_value_add_window_restricted_to_benchmark_coverage() -> None:
    # Mandate has 3 periods but the benchmark only covers the last two — the
    # mandate TWR must be recomputed over the benchmark's window only.
    periods = _periods(0.05, 0.10, 0.10)  # Aug→Sep, Sep→Oct, Oct→Nov
    bench = _bench("Late", [
        (date(2021, 9, 1), "100"),
        (date(2021, 10, 1), "110"),
        (date(2021, 11, 1), "121"),
    ])
    report = mb.build_report(periods, [bench])
    r = report.rows[0]
    assert r.periods == 2  # only Sep→Oct and Oct→Nov align
    assert r.window_start == date(2021, 9, 1)
    # Mandate over the window = 1.1*1.1-1 = 0.21 (the 0.05 Aug period excluded)
    assert r.mandate_twr is not None and abs(r.mandate_twr - 0.21) < 1e-9


def test_benchmark_with_no_overlap_is_skipped() -> None:
    periods = _periods(0.05)  # Aug→Sep 2021
    bench = _bench("Future", [
        (date(2030, 1, 1), "100"), (date(2030, 2, 1), "101"),
    ])
    report = mb.build_report(periods, [bench])
    assert report.rows == ()
    assert report.skipped == ("Future",)


# --- rendering ------------------------------------------------------------


def test_render_markdown_and_csv() -> None:
    periods = _periods(0.10, 0.10)
    bench = _bench("Global 60/40", [
        (date(2021, 8, 1), "100"),
        (date(2021, 9, 1), "105"),
        (date(2021, 10, 1), "110"),
    ])
    report = mb.build_report(periods, [bench])
    md = mb.render_markdown(report)
    assert "# Mandate value-add vs benchmarks" in md
    assert "Global 60/40" in md
    assert "Value-add p.a." in md
    rows = mb.render_csv_rows(report)
    assert rows[0][0] == "benchmark"
    assert rows[1][0] == "Global 60/40"


def test_render_empty_when_no_benchmarks() -> None:
    report = mb.build_report(_periods(0.1), [])
    md = mb.render_markdown(report)
    assert "No benchmark data" in md
