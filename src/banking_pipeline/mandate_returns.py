"""Mandate return scorecard — step 2: time- and money-weighted returns.

What the Pictet mandate actually *returned*, on two bases the user asked
for side by side:

* **net (equity) return** — the return on net worth (assets minus the
  negative Lombard cash). The loan drawdown is internal (cash up, debt up,
  net worth unchanged); the loan interest is a drag carried inside the
  return. This is the investor's true experience of their own capital.
* **gross (asset) return** — the return on the total asset book (the loan
  added back). A loan drawdown is treated as an external inflow and its
  use as an outflow, so leverage doesn't read as performance. The gap
  between the two is the leverage contribution.

For each basis two numbers are computed:

* **TWR** (time-weighted) — chained per-period Modified Dietz returns, so
  the *timing* of deposits/withdrawals is stripped out. The manager's
  scorecard, comparable to a benchmark (step 3).
* **MWR / XIRR** (money-weighted) — the single rate reconciling every
  dated external flow with the latest valuation. The investor's actual
  experience; reported for the net basis (the equity the user funds).

Two series feed it: the **statement valuations** (per portfolio, per date,
reusing :mod:`banking_pipeline.valuation`) and the **external capital
flows** read from the ledger — ``Expenses:Pic:*:Other`` (wires out) and
``Equity:Pic:*:Transfers`` (deposits), each ``F = −(posting amount)`` into
the portfolio. The opening capital never came through as an advice (it is
the first statement balance), so inception value is the first snapshot, not
a flow.

Completeness caveat: the flow series is only as good as what the ledger
tags. A deposit that arrived as a raw balance change with no advice would
be invisible and inflate that period's apparent return, so the report
flags any single period whose implied return exceeds
``_OUTSIZED_PERIOD_RETURN`` as a possible untagged flow. A reporting aid,
not advice.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.bean_query import QueryResult, run_query
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.report_format import gbp, money
from banking_pipeline.tax.uk.currency import RateGap, to_gbp
from banking_pipeline.valuation import (
    RawHolding,
    as_of,
    raw_from_statement,
    value_holdings,
)

_ZERO = Decimal(0)
_PICTET_PREFIX = "Assets:Pic:"

# A single sub-period return above this magnitude is implausible as real
# performance on a diversified book and almost certainly signals an
# untagged external flow — flagged for review, not silently trusted.
_OUTSIZED_PERIOD_RETURN = 0.20

# Per-posting external flows: deposits (Equity transfers) and wires out
# (the Other catch-all leg). ``F = −(posting amount)`` into the portfolio.
_FLOW_BQL = (
    "SELECT date, account, units(sum(position)) AS amount "
    'WHERE (account ~ "Equity:Pic" AND account ~ "Transfers") '
    'OR (account ~ "Expenses:Pic" AND account ~ ":Other") '
    "GROUP BY date, account ORDER BY date"
)


@dataclass(frozen=True)
class Snapshot:
    """A portfolio's value at one statement date, on both bases."""

    portfolio: str
    on_date: date
    net_value_gbp: Decimal  # net worth = assets − loan
    gross_value_gbp: Decimal  # total assets (loan added back)
    loan_gbp: Decimal  # ≤ 0; the negative (Lombard) cash total


@dataclass(frozen=True)
class Flow:
    """An external capital movement into (+) or out of (−) a portfolio."""

    portfolio: str
    on_date: date
    amount_gbp: Decimal  # signed: + into the portfolio, − out


@dataclass(frozen=True)
class PeriodReturn:
    start: date
    end: date
    begin_value_gbp: Decimal
    end_value_gbp: Decimal
    flow_gbp: Decimal  # net external flow over the period
    twr: float | None  # Modified Dietz return for the sub-period
    outsized: bool  # |twr| over the untagged-flow threshold


@dataclass(frozen=True)
class ReturnSeries:
    """One portfolio's (or the aggregate's) return on net + gross bases."""

    label: str
    inception: date | None
    latest: date | None
    net_value_gbp: Decimal  # latest net worth
    gross_value_gbp: Decimal  # latest total assets
    twr_net: float | None  # cumulative, since inception
    twr_gross: float | None
    twr_net_annualised: float | None
    twr_gross_annualised: float | None
    mwr_net: float | None  # XIRR on the net (equity) flows + latest value
    periods_net: tuple[PeriodReturn, ...]
    suspect_periods: tuple[PeriodReturn, ...]  # outsized — possible untagged flow


@dataclass(frozen=True)
class ReturnReport:
    aggregate: ReturnSeries
    per_portfolio: tuple[ReturnSeries, ...]
    rate_gaps: tuple[RateGap, ...]  # flows that couldn't be converted to GBP


# --- valuation snapshots ----------------------------------------------------


def build_snapshots(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> list[Snapshot]:
    """Per-(portfolio, date) value snapshots for the Pictet mandate.

    Cash is netted *within* each portfolio (not across), so each account's
    own Lombard balance is its own loan. Property / ISA pseudo-portfolios
    are excluded — this is the Pictet mandate's return."""

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))

    groups: dict[tuple[str, date], dict[str, RawHolding]] = defaultdict(dict)
    for r in raws:
        if not r.portfolio.startswith(_PICTET_PREFIX):
            continue
        groups[(r.portfolio, r.on_date)][r.key] = r

    out: list[Snapshot] = []
    for (portfolio, on_date), grp in groups.items():
        valued = value_holdings(
            list(grp.values()), commodities=commodities, rate_source=rate_source
        )
        loan = sum(
            (c.value_gbp for c in valued.cash if c.value_gbp < _ZERO), _ZERO
        )
        out.append(
            Snapshot(
                portfolio=portfolio,
                on_date=on_date,
                net_value_gbp=valued.net_worth_gbp,
                gross_value_gbp=valued.net_worth_gbp - loan,  # add the loan back
                loan_gbp=loan,
            )
        )
    out.sort(key=lambda s: (s.portfolio, s.on_date))
    return out


# --- external capital flows -------------------------------------------------


def query_flows(ledger: Path) -> QueryResult:
    """Run the external-flow query against ``ledger`` via ``bean-query``."""

    return run_query(ledger, _FLOW_BQL)


def _portfolio_from_flow_account(account: str) -> str:
    """``Expenses:Pic:K999999001:Other`` → ``Assets:Pic:K999999001`` — map a
    flow leg onto the matching asset portfolio key used by the snapshots."""

    parts = account.split(":")
    # parts[2] is the portfolio segment (K…/P…) for both Equity:Pic:<p>:…
    # and Expenses:Pic:<p>:… ; rebuild the Assets portfolio key.
    return f"{_PICTET_PREFIX}{parts[2]}" if len(parts) >= 3 else account


def _parse_amount(field: str) -> tuple[Decimal, str] | None:
    parts = field.split()
    if len(parts) != 2:
        return None
    try:
        return Decimal(parts[0].replace(",", "")), parts[1]
    except (ArithmeticError, ValueError):
        return None


def build_flows(
    result: QueryResult, *, rate_source: GbpRateSource
) -> tuple[list[Flow], list[RateGap]]:
    """Per-portfolio external flows in GBP. ``F = −(posting amount)`` so a
    deposit (negative equity credit) is +in and a wire-out (positive expense
    debit) is −out. An unconvertible flow is dropped and recorded."""

    flows: list[Flow] = []
    gaps: list[RateGap] = []
    for row in result.rows:
        if len(row) < 3:
            continue
        date_str, account, amount_field = row[0], row[1].strip(), row[2]
        parsed = _parse_amount(amount_field)
        if parsed is None:
            continue
        amount, ccy = parsed
        on_date = date.fromisoformat(date_str)
        value = to_gbp(amount, currency=ccy, on_date=on_date, source=rate_source)
        if value is None:
            gaps.append(RateGap.at(account, ccy, on_date))
            continue
        flows.append(
            Flow(
                portfolio=_portfolio_from_flow_account(account),
                on_date=on_date,
                amount_gbp=-value,  # into the portfolio
            )
        )
    return flows, gaps


# --- return maths -----------------------------------------------------------


def _modified_dietz(
    v0: Decimal, v1: Decimal, flows: list[Flow], start: date, end: date
) -> float | None:
    """Modified Dietz return over ``(start, end]``: each flow day-weighted by
    the fraction of the period it was invested. ``None`` if the weighted base
    is zero (no capital at risk)."""

    days = (end - start).days or 1
    net_flow = sum((f.amount_gbp for f in flows), _ZERO)
    weighted = sum(
        (float(f.amount_gbp) * ((end - f.on_date).days / days) for f in flows),
        0.0,
    )
    base = float(v0) + weighted
    if base <= 0:
        # No positive capital at risk over the period — a return on a zero or
        # negative base (e.g. a portfolio whose Lombard loan exceeds its
        # assets, so equity is negative) is meaningless. Suppressed, not
        # printed as a flipped-sign artefact.
        return None
    return (float(v1) - float(v0) - float(net_flow)) / base


def _chain(returns: list[float | None]) -> float | None:
    """Compound a list of sub-period returns (skipping gaps) into a cumulative
    return. ``None`` if no period is computable."""

    factor = 1.0
    any_period = False
    for r in returns:
        if r is None:
            continue
        factor *= 1.0 + r
        any_period = True
    return factor - 1.0 if any_period else None


def _annualise(cumulative: float | None, inception: date, latest: date) -> float | None:
    if cumulative is None:
        return None
    years = (latest - inception).days / 365.25
    if years <= 0:
        return cumulative
    return float((1.0 + cumulative) ** (1.0 / years)) - 1.0


def _xirr(cashflows: list[tuple[date, Decimal]]) -> float | None:
    """Money-weighted return: the rate ``r`` solving ``Σ cf / (1+r)^t = 0``,
    ``t`` in years from the first flow. Investor sign convention — deposits
    negative, withdrawals + ending value positive. Bisection (no SciPy);
    ``None`` when there is no sign change to bracket a root."""

    if len(cashflows) < 2:
        return None
    d0 = min(d for d, _ in cashflows)

    def npv(rate: float) -> float:
        return float(sum(
            float(a) / (1.0 + rate) ** ((d - d0).days / 365.25)
            for d, a in cashflows
        ))

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None  # no bracketed sign change
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if (f_lo > 0) != (f_mid > 0):
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def _series_for(
    label: str, snaps: list[Snapshot], flows: list[Flow]
) -> ReturnSeries:
    """Compute the net + gross TWR and net MWR for one value-snapshot series.

    Net basis uses ``net_value_gbp`` with the capital flows; gross basis uses
    ``gross_value_gbp`` (total assets) and additionally treats the change in
    the loan balance between snapshots as a financing flow, so a drawdown
    isn't counted as performance."""

    snaps = sorted(snaps, key=lambda s: s.on_date)
    if len(snaps) < 2:
        last_snap = snaps[-1] if snaps else None
        return ReturnSeries(
            label=label,
            inception=snaps[0].on_date if snaps else None,
            latest=last_snap.on_date if last_snap else None,
            net_value_gbp=last_snap.net_value_gbp if last_snap else _ZERO,
            gross_value_gbp=last_snap.gross_value_gbp if last_snap else _ZERO,
            twr_net=None, twr_gross=None,
            twr_net_annualised=None, twr_gross_annualised=None,
            mwr_net=None, periods_net=(), suspect_periods=(),
        )

    net_returns: list[float | None] = []
    gross_returns: list[float | None] = []
    period_objs: list[PeriodReturn] = []
    suspect: list[PeriodReturn] = []

    for prev, cur in zip(snaps, snaps[1:], strict=False):
        window = [f for f in flows if prev.on_date < f.on_date <= cur.on_date]
        r_net = _modified_dietz(
            prev.net_value_gbp, cur.net_value_gbp, window, prev.on_date, cur.on_date
        )
        # Gross basis: add the loan-principal change as a financing flow
        # (drawdown = inflow to the asset book). loan ≤ 0, so a bigger debt
        # makes (cur.loan − prev.loan) negative → +inflow.
        loan_flow = Flow(label, cur.on_date, -(cur.loan_gbp - prev.loan_gbp))
        r_gross = _modified_dietz(
            prev.gross_value_gbp, cur.gross_value_gbp,
            [*window, loan_flow], prev.on_date, cur.on_date,
        )
        net_returns.append(r_net)
        gross_returns.append(r_gross)
        net_flow = sum((f.amount_gbp for f in window), _ZERO)
        outsized = r_net is not None and abs(r_net) > _OUTSIZED_PERIOD_RETURN
        pr = PeriodReturn(
            start=prev.on_date, end=cur.on_date,
            begin_value_gbp=prev.net_value_gbp, end_value_gbp=cur.net_value_gbp,
            flow_gbp=net_flow, twr=r_net, outsized=outsized,
        )
        period_objs.append(pr)
        if outsized:
            suspect.append(pr)

    # Re-anchor inception past any *leading* suspect period: the opening
    # capital lands untagged during the first interval (the account is
    # near-empty before it), so that period is a funding event, not
    # performance. Start the series at the first snapshot from which a clean
    # period runs. A still-suspect period mid-series is nulled (treated as an
    # untagged flow, not credited to the manager) but reported below.
    start_idx = next(
        (i for i, p in enumerate(period_objs) if not p.outsized), len(period_objs)
    )
    net_eff = [
        None if period_objs[i].outsized else net_returns[i]
        for i in range(start_idx, len(net_returns))
    ]
    gross_eff = [
        None if period_objs[i].outsized else gross_returns[i]
        for i in range(start_idx, len(gross_returns))
    ]
    anchor = snaps[start_idx] if start_idx < len(snaps) else snaps[-1]
    inception, latest = anchor.on_date, snaps[-1].on_date
    twr_net = _chain(net_eff)
    twr_gross = _chain(gross_eff)

    # MWR (net basis): inception value out, each capital flow, ending value
    # in. Only meaningful from positive equity (a negative-equity account has
    # no capital base to earn a money-weighted return on).
    mwr_net: float | None = None
    if anchor.net_value_gbp > _ZERO:
        cashflows: list[tuple[date, Decimal]] = [(inception, -anchor.net_value_gbp)]
        cashflows += [(f.on_date, f.amount_gbp) for f in flows
                      if inception < f.on_date <= latest]
        cashflows.append((latest, snaps[-1].net_value_gbp))
        mwr_net = _xirr(cashflows)

    return ReturnSeries(
        label=label,
        inception=inception,
        latest=latest,
        net_value_gbp=snaps[-1].net_value_gbp,
        gross_value_gbp=snaps[-1].gross_value_gbp,
        twr_net=twr_net,
        twr_gross=twr_gross,
        twr_net_annualised=_annualise(twr_net, inception, latest),
        twr_gross_annualised=_annualise(twr_gross, inception, latest),
        mwr_net=mwr_net,
        periods_net=tuple(period_objs),
        suspect_periods=tuple(suspect),
    )


def _aggregate_snapshots(snaps: list[Snapshot]) -> list[Snapshot]:
    """Combine per-portfolio snapshots into a whole-mandate series via the
    as-of forward-fill (each portfolio contributes its latest snapshot on or
    before each date), summing the bases — the same shape ``net_worth`` uses.
    """

    by_portfolio: dict[str, list[Snapshot]] = defaultdict(list)
    for s in snaps:
        by_portfolio[s.portfolio].append(s)
    for lst in by_portfolio.values():
        lst.sort(key=lambda s: s.on_date)

    out: list[Snapshot] = []
    for d in sorted({s.on_date for s in snaps}):
        net = gross = loan = _ZERO
        for lst in by_portfolio.values():
            chosen = as_of(lst, d, key=lambda s: s.on_date)
            if chosen is not None:
                net += chosen.net_value_gbp
                gross += chosen.gross_value_gbp
                loan += chosen.loan_gbp
        out.append(Snapshot("Pictet (all)", d, net, gross, loan))
    return out


def build_report(
    statements: list[tuple[str, str]],
    flow_result: QueryResult,
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> ReturnReport:
    """Assemble the whole-mandate and per-portfolio return series."""

    snaps = build_snapshots(
        statements, commodities=commodities, rate_source=rate_source
    )
    flows, gaps = build_flows(flow_result, rate_source=rate_source)

    by_portfolio: dict[str, list[Snapshot]] = defaultdict(list)
    for s in snaps:
        by_portfolio[s.portfolio].append(s)
    flows_by_portfolio: dict[str, list[Flow]] = defaultdict(list)
    for f in flows:
        flows_by_portfolio[f.portfolio].append(f)

    per_portfolio = tuple(
        _series_for(p, by_portfolio[p], flows_by_portfolio.get(p, []))
        for p in sorted(by_portfolio)
    )
    aggregate = _series_for(
        "Pictet (all)", _aggregate_snapshots(snaps), flows
    )
    return ReturnReport(
        aggregate=aggregate, per_portfolio=per_portfolio, rate_gaps=tuple(gaps)
    )


# --- rendering --------------------------------------------------------------


def _pctf(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _portfolio_label(key: str) -> str:
    """``Assets:Pic:K999999001`` → ``K999999001`` for display."""

    return key.rsplit(":", 1)[-1]


def _series_row(s: ReturnSeries, label: str) -> str:
    return (
        f"| {label} | {gbp(s.net_value_gbp)} | {_pctf(s.twr_net)} "
        f"| {_pctf(s.twr_net_annualised)} | {_pctf(s.mwr_net)} "
        f"| {_pctf(s.twr_gross)} | {_pctf(s.twr_gross_annualised)} |"
    )


def render_markdown(report: ReturnReport) -> str:
    agg = report.aggregate
    span = (
        f"{agg.inception} → {agg.latest}"
        if agg.inception and agg.latest
        else "—"
    )
    lines = [
        "# Mandate returns — time- & money-weighted",
        "",
        f"Pictet mandate return over **{span}**, on two bases side by side: "
        "**net** (your equity — assets minus the Lombard loan, with the loan's "
        "interest as an internal drag) and **gross** (the total asset book, "
        "the loan added back and its principal moves treated as flows so "
        "leverage isn't counted as performance). TWR strips out deposit "
        "timing (the manager's scorecard); MWR/XIRR is your actual "
        "money-weighted experience. A reporting aid, not advice.",
        "",
        "| Scope | Latest net worth | TWR (net) | TWR p.a. (net) | MWR (net) "
        "| TWR (gross) | TWR p.a. (gross) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        _series_row(agg, "**Pictet (all)**"),
    ]
    for s in report.per_portfolio:
        lines.append(_series_row(s, _portfolio_label(s.label)))
    lines.append("")
    lines += [
        "*Net vs gross TWR p.a.* shows the leverage contribution: where net "
        "exceeds gross the loan amplified gains; where it trails, leverage "
        "dragged. *MWR vs TWR* shows how deposit/withdrawal timing helped or "
        "hurt your realised return versus the manager's underlying one.",
        "",
    ]
    if any(s.net_value_gbp < _ZERO for s in report.per_portfolio):
        lines += [
            "> An account whose Lombard loan exceeds its assets has negative "
            "equity, so its **net** return is undefined and shown `—` (only "
            "the whole-mandate net, where the loan nets against the other "
            "account's assets, is meaningful). Its gross figure values the "
            "asset side alone.",
            "",
        ]

    suspects = agg.suspect_periods
    if suspects:
        lines += [
            "## ⚠️ Periods to review — possible untagged flows",
            "",
            "These sub-periods show an implied return too large to be real "
            f"performance (|return| over {_OUTSIZED_PERIOD_RETURN * 100:.0f}% "
            "in one period), which usually means a deposit or withdrawal that "
            "wasn't tagged in the ledger. A *leading* flagged period is the "
            "opening-capital arrival, so the series is anchored to start "
            "after it; a *mid-series* one is excluded from the TWR (not "
            "credited as performance). Tagging these flows would let them "
            "re-enter the figures. Check these statement intervals:",
            "",
            "| Period | Begin | End | Net flow | Implied return |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for p in suspects:
            lines.append(
                f"| {p.start} → {p.end} | {gbp(p.begin_value_gbp)} "
                f"| {gbp(p.end_value_gbp)} | {gbp(p.flow_gbp)} "
                f"| {_pctf(p.twr)} |"
            )
        lines.append("")

    if report.rate_gaps:
        lines += [
            "## ⚠️ Flows excluded — missing GBP rate",
            "",
            "These external flows couldn't be converted to GBP, so the return "
            "base is incomplete; add the month/currency to "
            "`data/fx/hmrc-monthly-average.csv`:",
            "",
            *[
                f"- {g.currency} {g.month} ({g.isin})"
                for g in sorted(
                    set(report.rate_gaps), key=lambda g: (g.month, g.currency)
                )
            ],
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: ReturnReport) -> list[list[str]]:
    rows: list[list[str]] = [[
        "scope", "inception", "latest", "net_worth_gbp", "gross_assets_gbp",
        "twr_net", "twr_net_annualised", "mwr_net",
        "twr_gross", "twr_gross_annualised",
    ]]

    def _row(s: ReturnSeries, label: str) -> list[str]:
        def f(v: float | None) -> str:
            return "" if v is None else f"{v:.6f}"
        return [
            label,
            s.inception.isoformat() if s.inception else "",
            s.latest.isoformat() if s.latest else "",
            money(s.net_value_gbp), money(s.gross_value_gbp),
            f(s.twr_net), f(s.twr_net_annualised), f(s.mwr_net),
            f(s.twr_gross), f(s.twr_gross_annualised),
        ]

    rows.append(_row(report.aggregate, "Pictet (all)"))
    for s in report.per_portfolio:
        rows.append(_row(s, _portfolio_label(s.label)))
    return rows
