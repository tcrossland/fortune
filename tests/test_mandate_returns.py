"""Mandate returns (step 2): the holdings-based market-gain / TWR / XIRR
maths and rendering. The return is computed from the statement holdings
(units × price moves), so no ledger flows are involved.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline import mandate_returns as mr
from banking_pipeline.models import DocumentType, Transaction


def dec(x: str) -> Decimal:
    return Decimal(x)


def _snap(
    d: date,
    *,
    net: str,
    loan: str = "0",
    positions: dict[str, tuple[str, str]] | None = None,
) -> mr.Snapshot:
    net_v = dec(net)
    loan_v = dec(loan)
    pos = {
        k: (dec(q), dec(v)) for k, (q, v) in (positions or {}).items()
    }
    return mr.Snapshot(
        portfolio="Assets:Pic:K1",
        on_date=d,
        positions=pos,
        net_value_gbp=net_v,
        gross_value_gbp=net_v - loan_v,
        loan_gbp=loan_v,
    )


# --- market gain ----------------------------------------------------------


def test_market_gain_price_move_only() -> None:
    # 100 units, value 1000 → 1100: unit 10 → 11, gain = 100 × 1 = 100.
    prev = _snap(date(2024, 1, 1), net="1000", positions={"X": ("100", "1000")})
    cur = _snap(date(2024, 2, 1), net="1100", positions={"X": ("100", "1100")})
    assert mr._market_gain(prev, cur) == dec("100")


def test_market_gain_ignores_unit_change_a_deposit() -> None:
    # Units bought (100 → 150) at a flat unit price (10): the deposit must
    # NOT count as a market gain — gain stays 0 on the held units.
    prev = _snap(date(2024, 1, 1), net="1000", positions={"X": ("100", "1000")})
    cur = _snap(date(2024, 2, 1), net="1500", positions={"X": ("150", "1500")})
    assert mr._market_gain(prev, cur) == dec("0")


def test_market_gain_excludes_positions_not_in_both() -> None:
    # A holding sold (only in prev) or bought (only in cur) contributes no
    # market gain — its whole value change is a trade/flow.
    prev = _snap(date(2024, 1, 1), net="1000", positions={"SOLD": ("10", "1000")})
    cur = _snap(date(2024, 2, 1), net="2000", positions={"NEW": ("10", "2000")})
    assert mr._market_gain(prev, cur) == dec("0")


# --- chaining + annualising -----------------------------------------------


def test_chain_compounds_and_skips_gaps() -> None:
    assert abs(mr._chain([0.10, None, 0.05]) - 0.155) < 1e-9
    assert mr._chain([None, None]) is None


def test_annualise_two_years() -> None:
    r = mr._annualise(0.21, date(2022, 1, 1), date(2024, 1, 1))
    assert r is not None and abs(r - 0.10) < 1e-2


# --- XIRR -----------------------------------------------------------------


def test_xirr_simple_doubling() -> None:
    r = mr._xirr([(date(2024, 1, 1), dec("-100")), (date(2025, 1, 1), dec("200"))])
    # 2024 is a leap year (366/365.25 yr), so the rate is a touch under 100%.
    assert r is not None and abs(r - 1.0) < 1e-2


def test_xirr_no_sign_change_is_none() -> None:
    assert mr._xirr([(date(2024, 1, 1), dec("-1")), (date(2025, 1, 1), dec("-1"))]) is None


# --- series: net vs gross, leverage, gap-immunity --------------------------


def test_series_price_return_unlevered() -> None:
    # One holding, no loan: 10% price move → net and gross TWR both 10%.
    snaps = [
        _snap(date(2024, 1, 1), net="1000", positions={"X": ("100", "1000")}),
        _snap(date(2025, 1, 1), net="1100", positions={"X": ("100", "1100")}),
    ]
    s, _ = mr._series_for("Assets:Pic:K1", snaps)
    assert s.twr_net is not None and abs(s.twr_net - 0.10) < 1e-9
    assert s.twr_gross is not None and abs(s.twr_gross - 0.10) < 1e-9


def test_series_leverage_amplifies_net_above_gross() -> None:
    # Assets 2000 (a 1000 holding... here gross 2000) financed by a 1000
    # loan → equity 1000. A 100 market gain is 100/1000 = 10% on equity but
    # 100/2000 = 5% on assets.
    snaps = [
        _snap(date(2024, 1, 1), net="1000", loan="-1000",
              positions={"X": ("100", "2000")}),
        _snap(date(2025, 1, 1), net="1100", loan="-1000",
              positions={"X": ("100", "2100")}),
    ]
    s, _ = mr._series_for("Assets:Pic:K1", snaps)
    assert s.twr_net is not None and abs(s.twr_net - 0.10) < 1e-9
    assert s.twr_gross is not None and abs(s.twr_gross - 0.05) < 1e-9
    assert s.twr_net > s.twr_gross


def test_series_deposit_is_gap_immune_and_detected() -> None:
    # A pure deposit deployed into the holding (units 100 → 200 at flat unit
    # price 10): TWR must be ~0 (no price move), and the +1000 deposit is
    # surfaced as a detected inflow — without any ledger tag.
    snaps = [
        _snap(date(2024, 1, 1), net="1000", positions={"X": ("100", "1000")}),
        _snap(date(2024, 2, 1), net="2000", positions={"X": ("200", "2000")}),
    ]
    s, flows = mr._series_for("Assets:Pic:K1", snaps)
    assert s.twr_net is not None and abs(s.twr_net) < 1e-9  # not +100%
    assert len(flows) == 1
    assert flows[0].amount_gbp == dec("1000")  # inferred deposit


def test_series_negative_equity_net_suppressed() -> None:
    # Loan exceeds assets → negative equity. Net TWR/MWR suppressed; gross
    # (positive asset base) still computed.
    snaps = [
        _snap(date(2024, 1, 1), net="-500", loan="-2000",
              positions={"X": ("100", "1500")}),
        _snap(date(2025, 1, 1), net="-450", loan="-2000",
              positions={"X": ("100", "1550")}),
    ]
    s, _ = mr._series_for("Assets:Pic:P1", snaps)
    assert s.twr_net is None
    assert s.mwr_net is None
    assert s.twr_gross is not None


# --- report assembly + rendering ------------------------------------------


# --- distribution income (the fund-payout fix) ----------------------------


def _div(*, isin: str, account: str, amount: str, on: date) -> Transaction:
    return Transaction(
        trade_date=on, booking_date=on, narration="Dividend", title="Dividend",
        currency="GBP", amount=dec(amount), isin=isin, account_number=account,
        document_type=DocumentType.DIVIDEND_NOTICE, source_path=Path("d.pdf"),
    )


def test_distribution_income_maps_pictet_dividend_to_portfolio() -> None:
    ev = mr.distribution_income(
        [_div(isin="LU0000000000", account="K-999999.001", amount="500",
              on=date(2024, 6, 15))],
        rate_source=_NullRates(),
    )
    assert ev == {"Assets:Pic:K999999001": [(date(2024, 6, 15), dec("500"))]}


def test_distribution_income_excludes_non_distribution_rows() -> None:
    # A redemption (not a distribution doctype) and a dividend with no ISIN
    # are both excluded — only fund payouts carrying an ISIN count.
    redemption = Transaction(
        trade_date=date(2024, 6, 1), booking_date=date(2024, 6, 1),
        narration="Redemption", title="Redemption", currency="GBP",
        amount=dec("1000"), isin="LU0000000000", account_number="K-999999.001",
        quantity=dec("-10"), document_type=DocumentType.REDEMPTION_NOTICE,
        source_path=Path("r.pdf"),
    )
    no_isin = Transaction(
        trade_date=date(2024, 6, 1), booking_date=date(2024, 6, 1),
        narration="Interest", title="Interest", currency="GBP", amount=dec("5"),
        account_number="K-999999.001",
        document_type=DocumentType.DIVIDEND_NOTICE, source_path=Path("i.pdf"),
    )
    assert mr.distribution_income(
        [redemption, no_isin], rate_source=_NullRates()
    ) == {}


def test_income_in_windows_and_portfolio_filter() -> None:
    ev = {
        "Assets:Pic:K1": [(date(2024, 1, 15), dec("100")), (date(2024, 3, 1), dec("50"))],
        "Assets:Pic:P1": [(date(2024, 1, 20), dec("30"))],
    }
    # (2024-01-01, 2024-02-01] over the whole mandate: K's 100 + P's 30.
    assert mr._income_in(ev, None, date(2024, 1, 1), date(2024, 2, 1)) == dec("130")
    # restricted to K1: only its 100.
    assert mr._income_in(
        ev, {"Assets:Pic:K1"}, date(2024, 1, 1), date(2024, 2, 1)
    ) == dec("100")
    # boundary: an event on `start` is excluded, on `end` included.
    assert mr._income_in(ev, None, date(2024, 1, 15), date(2024, 3, 1)) == dec("80")


def test_series_distribution_counts_as_return_not_flow() -> None:
    # Flat unit price (no market gain) but a 50 cash distribution lands, so
    # net worth rises 1000 → 1050. Without income the 50 reads as a spurious
    # inflow and TWR is 0; with income it's a 5% return and no flow.
    snaps = [
        _snap(date(2024, 1, 1), net="1000", positions={"X": ("100", "1000")}),
        _snap(date(2024, 2, 1), net="1050", positions={"X": ("100", "1000")}),
    ]
    events = {"Assets:Pic:K1": [(date(2024, 1, 20), dec("50"))]}

    s0, f0 = mr._series_for("Assets:Pic:K1", snaps)
    assert s0.twr_net is not None and abs(s0.twr_net) < 1e-9
    assert f0[0].amount_gbp == dec("50")  # spurious inferred deposit

    s1, f1 = mr._series_for(
        "Assets:Pic:K1", snaps,
        income_events=events, income_portfolios={"Assets:Pic:K1"},
    )
    assert s1.twr_net is not None and abs(s1.twr_net - 0.05) < 1e-9
    assert abs(f1[0].amount_gbp) < 1e-9  # distribution no longer a flow


def test_series_income_skipped_without_tracked_positions() -> None:
    # A portfolio with no tracked positions (value is residual cash only, as
    # with the P mandate's unvalued by-name holdings): income must NOT be
    # folded in — dividing it by that tiny base is meaningless. The £50 rise
    # stays an inferred flow, return 0.
    snaps = [
        _snap(date(2024, 1, 1), net="250", positions={}),
        _snap(date(2024, 2, 1), net="300", positions={}),
    ]
    events = {"Assets:Pic:K1": [(date(2024, 1, 20), dec("50"))]}
    s, flows = mr._series_for(
        "Assets:Pic:K1", snaps,
        income_events=events, income_portfolios={"Assets:Pic:K1"},
    )
    assert s.twr_net is not None and abs(s.twr_net) < 1e-9
    assert flows[0].amount_gbp == dec("50")  # unchanged — still a flow


def test_build_report_empty_renders_cleanly() -> None:
    report = mr.build_report([], commodities={}, rate_source=_NullRates())
    md = mr.render_markdown(report)
    assert "# Mandate returns" in md
    rows = mr.render_csv_rows(report)
    assert rows[0][0] == "scope"
    assert rows[1][0] == "Pictet (all)"


def test_render_columns_and_detected_flows() -> None:
    s = mr.ReturnSeries(
        label="Assets:Pic:K1", inception=date(2024, 1, 1), latest=date(2025, 1, 1),
        net_value_gbp=dec("1100"), gross_value_gbp=dec("2100"),
        twr_net=0.10, twr_gross=0.05,
        twr_net_annualised=0.10, twr_gross_annualised=0.05, mwr_net=0.09,
    )
    flows = (mr.DetectedFlow("Pictet (all)", date(2024, 6, 1), dec("750000")),)
    report = mr.ReturnReport(aggregate=s, per_portfolio=(s,), detected_flows=flows)
    md = mr.render_markdown(report)
    assert "TWR (net)" in md and "TWR (gross)" in md and "MWR (net)" in md
    assert "10.0%" in md and "5.0%" in md
    assert "Detected movements" in md
    assert "deposit" in md and "£750,000.00" in md


class _NullRates:
    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return Decimal(1) if currency.upper() == "GBP" else None


# --- empty-snapshot drop (the gap bridge) ---------------------------------


def test_build_snapshots_drops_empty_gap_statement() -> None:
    # An empty statement text (no holdings, no value) must not create a
    # snapshot — otherwise it reads as the book leaving and returning.
    empty = "Estimated Asset Statement as at 30.11.2022\nAccount No. K-999999.001\n"
    snaps = mr.build_snapshots(
        [(empty, "empty.pdf")], commodities={}, rate_source=_NullRates()
    )
    assert snaps == []
