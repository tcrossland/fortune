"""Mandate returns (step 2): the TWR/Modified-Dietz/XIRR maths, flow
extraction, snapshot bases, and rendering.

The bean-query subprocess is not exercised — `build_flows` /
`build_report` take a `QueryResult`. A `FakeRates` stands in for the GBP
rate source. The maths is checked against hand-computed values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline import mandate_returns as mr
from banking_pipeline.bean_query import QueryResult


class FakeRates:
    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return {"GBP": Decimal(1), "EUR": Decimal("0.85")}.get(currency.upper())


def dec(x: str) -> Decimal:
    return Decimal(x)


# --- Modified Dietz -------------------------------------------------------


def test_modified_dietz_no_flows() -> None:
    # 100 → 110 over a period, no flows → 10%.
    r = mr._modified_dietz(
        dec("100"), dec("110"), [], date(2024, 1, 1), date(2024, 2, 1)
    )
    assert r is not None and abs(r - 0.10) < 1e-9


def test_modified_dietz_midpoint_flow_weighted() -> None:
    # 100 start, a +100 deposit exactly halfway, end 220.
    # base = 100 + 100*0.5 = 150; gain = 220-100-100 = 20; r = 20/150.
    start, end = date(2024, 1, 1), date(2024, 1, 31)
    mid = date(2024, 1, 16)  # 15 of 30 days remaining
    flow = mr.Flow("p", mid, dec("100"))
    r = mr._modified_dietz(dec("100"), dec("220"), [flow], start, end)
    assert r is not None and abs(r - (20.0 / 150.0)) < 1e-3


def test_modified_dietz_zero_base_is_none() -> None:
    assert mr._modified_dietz(
        dec("0"), dec("0"), [], date(2024, 1, 1), date(2024, 2, 1)
    ) is None


# --- chaining + annualising -----------------------------------------------


def test_chain_compounds_and_skips_gaps() -> None:
    # (1.1)(1.05) - 1 = 0.155; a None period is skipped.
    assert abs(mr._chain([0.10, None, 0.05]) - 0.155) < 1e-9
    assert mr._chain([None, None]) is None


def test_annualise_two_years() -> None:
    # 21% over ~2 years → ~10% p.a.
    r = mr._annualise(0.21, date(2022, 1, 1), date(2024, 1, 1))
    assert r is not None and abs(r - 0.10) < 1e-2


# --- XIRR -----------------------------------------------------------------


def test_xirr_simple_doubling() -> None:
    # Invest 100, get 200 back one year later → 100% IRR.
    r = mr._xirr([(date(2024, 1, 1), dec("-100")), (date(2025, 1, 1), dec("200"))])
    # 2024 is a leap year (366/365.25 yr), so the rate is a touch under 100%.
    assert r is not None and abs(r - 1.0) < 1e-2


def test_xirr_flat() -> None:
    r = mr._xirr([(date(2024, 1, 1), dec("-100")), (date(2025, 1, 1), dec("100"))])
    assert r is not None and abs(r) < 1e-3


def test_xirr_no_sign_change_is_none() -> None:
    # All same sign → no bracketed root.
    assert mr._xirr([(date(2024, 1, 1), dec("-1")), (date(2025, 1, 1), dec("-1"))]) is None


# --- flow extraction ------------------------------------------------------


def test_build_flows_signs() -> None:
    # Equity transfer (deposit, negative posting) → +in.
    # Expenses Other (wire out, positive posting) → −out.
    result = QueryResult(rows=[
        ["2024-01-10", "Equity:Pic:K1:Transfers", "-50000 GBP"],
        ["2024-02-10", "Expenses:Pic:K1:Other", "20000 GBP"],
        ["2024-03-10", "Expenses:Pic:P1:Other", "10000 EUR"],
    ])
    flows, gaps = mr.build_flows(result, rate_source=FakeRates())
    assert not gaps
    by_date = {f.on_date: f for f in flows}
    assert by_date[date(2024, 1, 10)].amount_gbp == dec("50000")     # in
    assert by_date[date(2024, 2, 10)].amount_gbp == dec("-20000")    # out
    # EUR converted at 0.85 then negated.
    assert by_date[date(2024, 3, 10)].amount_gbp == dec("-8500")
    assert by_date[date(2024, 1, 10)].portfolio == "Assets:Pic:K1"
    assert by_date[date(2024, 3, 10)].portfolio == "Assets:Pic:P1"


def test_build_flows_missing_rate_is_gap() -> None:
    result = QueryResult(rows=[
        ["2024-01-10", "Expenses:Pic:K1:Other", "100 JPY"],  # no rate
    ])
    flows, gaps = mr.build_flows(result, rate_source=FakeRates())
    assert not flows
    assert len(gaps) == 1 and gaps[0].currency == "JPY"


# --- series: net vs gross, leverage ---------------------------------------


def _snap(d: date, net: str, loan: str) -> mr.Snapshot:
    net_v = dec(net)
    loan_v = dec(loan)
    return mr.Snapshot("Assets:Pic:K1", d, net_v, net_v - loan_v, loan_v)


def test_series_unlevered_net_equals_gross() -> None:
    # No loan, no flows: net and gross TWR identical, MWR ≈ TWR.
    snaps = [
        _snap(date(2024, 1, 1), "1000", "0"),
        _snap(date(2025, 1, 1), "1100", "0"),
    ]
    s = mr._series_for("Assets:Pic:K1", snaps, [])
    assert s.twr_net is not None and abs(s.twr_net - 0.10) < 1e-6
    assert s.twr_gross is not None and abs(s.twr_gross - 0.10) < 1e-6
    assert s.mwr_net is not None and abs(s.mwr_net - 0.10) < 1e-2


def test_series_leverage_amplifies_net_above_gross() -> None:
    # Assets 2000 financed by a 1000 loan → equity 1000. Assets rise 10%
    # (2000→2200), loan flat: equity 1000→1200 = +20%. Net TWR (20%) should
    # exceed gross TWR (10%).
    snaps = [
        _snap(date(2024, 1, 1), "1000", "-1000"),  # net 1000, gross 2000
        _snap(date(2025, 1, 1), "1200", "-1000"),  # net 1200, gross 2200
    ]
    s = mr._series_for("Assets:Pic:K1", snaps, [])
    assert s.twr_net is not None and s.twr_gross is not None
    assert abs(s.twr_net - 0.20) < 1e-6
    assert abs(s.twr_gross - 0.10) < 1e-6
    assert s.twr_net > s.twr_gross


def test_series_drawdown_not_counted_as_gross_performance() -> None:
    # A pure loan drawdown: cash +1000, loan −1000, net worth unchanged.
    # Gross assets jump 1000→2000 but it's financing, not performance, so
    # gross TWR ≈ 0.
    snaps = [
        _snap(date(2024, 1, 1), "1000", "0"),       # net 1000, gross 1000
        _snap(date(2024, 7, 1), "1000", "-1000"),   # net 1000, gross 2000
    ]
    s = mr._series_for("Assets:Pic:K1", snaps, [])
    assert s.twr_gross is not None and abs(s.twr_gross) < 1e-6
    assert s.twr_net is not None and abs(s.twr_net) < 1e-6


def test_series_flags_outsized_period() -> None:
    # A 1000→2000 jump with no tagged flow → +100% implied, flagged.
    snaps = [
        _snap(date(2024, 1, 1), "1000", "0"),
        _snap(date(2024, 2, 1), "2000", "0"),
    ]
    s = mr._series_for("Assets:Pic:K1", snaps, [])
    assert len(s.suspect_periods) == 1
    assert s.suspect_periods[0].outsized


def test_series_reanchors_past_leading_suspect() -> None:
    # Account opens near-empty; capital lands untagged in the first interval
    # (tiny → big = outsized), then grows 10%. Inception re-anchors to the
    # second snapshot and the outsized first period is excluded, so TWR is
    # the clean 10%, not the artefact.
    snaps = [
        _snap(date(2021, 8, 1), "5000", "0"),       # near-empty open
        _snap(date(2021, 9, 1), "1000000", "0"),    # capital arrived (outsized)
        _snap(date(2022, 9, 1), "1100000", "0"),    # +10%
    ]
    s = mr._series_for("Assets:Pic:K1", snaps, [])
    assert s.inception == date(2021, 9, 1)  # re-anchored past the funding
    assert s.twr_net is not None and abs(s.twr_net - 0.10) < 1e-6
    assert len(s.suspect_periods) == 1  # the funding period still reported


def test_series_negative_equity_net_suppressed() -> None:
    # The loan exceeds assets throughout → negative equity. Net TWR/MWR are
    # undefined (suppressed); the gross (asset) figure is still computed.
    snaps = [
        _snap(date(2024, 1, 1), "-500", "-2000"),  # net −500, gross 1500
        _snap(date(2025, 1, 1), "-400", "-2000"),  # net −400, gross 1600
    ]
    s = mr._series_for("Assets:Pic:P1", snaps, [])
    assert s.twr_net is None
    assert s.mwr_net is None
    assert s.twr_gross is not None  # gross assets are positive → computable


# --- report assembly + rendering ------------------------------------------


def _statements_unused() -> list[tuple[str, str]]:
    return []


def test_build_report_aggregates_and_renders() -> None:
    # Drive build_report through build_snapshots with no statements (empty),
    # so the aggregate is empty but flows still parse and render cleanly.
    flow_result = QueryResult(rows=[
        ["2024-02-10", "Expenses:Pic:K1:Other", "100 JPY"],  # gap
    ])
    report = mr.build_report(
        _statements_unused(), flow_result,
        commodities={}, rate_source=FakeRates(),
    )
    md = mr.render_markdown(report)
    assert "# Mandate returns" in md
    assert "missing GBP rate" in md  # the JPY flow gap surfaces
    rows = mr.render_csv_rows(report)
    assert rows[0][0] == "scope"
    assert rows[1][0] == "Pictet (all)"


def test_render_columns_present() -> None:
    snaps = [
        _snap(date(2024, 1, 1), "1000", "-1000"),
        _snap(date(2025, 1, 1), "1200", "-1000"),
    ]
    s = mr._series_for("Assets:Pic:K1", snaps, [])
    report = mr.ReturnReport(aggregate=s, per_portfolio=(s,), rate_gaps=())
    md = mr.render_markdown(report)
    assert "TWR (net)" in md and "TWR (gross)" in md and "MWR (net)" in md
    assert "20.0%" in md  # net TWR
    assert "10.0%" in md  # gross TWR
