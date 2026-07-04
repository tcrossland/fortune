"""Net-worth-over-time report.

Values each statement's valuation snapshot at its own date (reusing the
concentration valuation — securities at ``qty × mark``, cash netted by
currency, converted to GBP at the statement date), then assembles a
combined net-worth timeline across portfolios.

Portfolios are statemented on different cadences (Pictet monthly, the
Vanguard ISA less often), so the combined net worth at a given date uses
each portfolio's latest snapshot **on or before** that date (an as-of
forward-fill). A point therefore appears at every statement date, and the
net worth steps as each new statement arrives.

A recognised **nil** statement (a Vanguard ISA whose current-column account
total is £0.00) retires its portfolio at the drain date, via a zero-value
snapshot (:func:`~banking_pipeline.valuation.drained_portfolio_snapshot`), so
a wound-down account doesn't linger at its last non-empty value. Residual
caveat: a portfolio that simply *stops* statementing — with no closing nil
statement — still keeps contributing its last snapshot until superseded.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.property import Property
from banking_pipeline.report_format import (
    gbp,
    missing_price_lines,
    money,
    rate_gap_lines,
    unclassified_lines,
)
from banking_pipeline.tax.uk.currency import RateGap
from banking_pipeline.valuation import (
    RawHolding,
    as_of,
    drained_portfolio_snapshot,
    property_raws,
    raw_from_statement,
    value_holdings,
)

_ZERO = Decimal(0)


@dataclass(frozen=True)
class _Snapshot:
    portfolio: str
    on_date: date
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal
    net_worth_gbp: Decimal
    missing_prices: tuple[str, ...]  # unvaluable holdings in this snapshot


@dataclass(frozen=True)
class NetWorthPoint:
    on_date: date
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal
    net_worth_gbp: Decimal
    change_gbp: Decimal | None  # net-worth change vs the previous point
    portfolios: int  # how many portfolios contributed (as-of)


@dataclass(frozen=True)
class NetWorthTimeline:
    points: tuple[NetWorthPoint, ...]
    rate_gaps: tuple[RateGap, ...]  # snapshots that couldn't fully convert
    missing_prices: tuple[str, ...]
    unclassified: tuple[str, ...]  # valued but no commodities.toml metadata


def build_timeline(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    properties: list[Property] | None = None,
    monthly: bool = False,
) -> NetWorthTimeline:
    """Build the net-worth timeline from ``(text, source-name)`` pairs.

    ``properties`` (off-ledger residential property) each become a
    pseudo-portfolio contributing a snapshot per valuation date, so they
    join the timeline via the same as-of forward-fill.

    ``monthly`` resamples the timeline onto a first-of-month grid instead of
    emitting a point per raw statement date — see :func:`_timeline_from_raw`."""

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))
        drained = drained_portfolio_snapshot(text)
        if drained is not None:
            raws.append(drained)
    raws.extend(property_raws(properties or []))
    return _timeline_from_raw(
        raws, commodities=commodities, rate_source=rate_source, monthly=monthly
    )


def _month_start_grid(first: date, last: date) -> list[date]:
    """First-of-month dates from ``first``'s month through ``last``'s month,
    inclusive — the monthly resample grid."""

    out: list[date] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _timeline_from_raw(
    raws: list[RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    monthly: bool = False,
) -> NetWorthTimeline:
    # One snapshot per (portfolio, statement date), valued at that date.
    # Dedupe by commodity within a snapshot: two statements can share an
    # "as at" date (a monthly and a quarterly/annual both dated to the same
    # period end), and counting the same holding from both would double the
    # valuation.
    #
    # ``monthly`` resamples the emitted points onto a first-of-month grid
    # (each valued by the same as-of forward-fill) instead of one point per
    # raw statement date. Portfolios statement on mixed cadences — Pictet
    # month-end (dated to the 1st of the next month), the Vanguard ISA and
    # property valuations mid-month — so the default event-driven grid shows
    # spurious mid-month rows where only one portfolio refreshed. The monthly
    # grid keeps the fresh-Pictet first-of-month points and folds each
    # mid-month update into the next one. Trade-off: an update in the current
    # in-progress month isn't shown until that month's first-of-month anchor.
    groups: dict[tuple[str, date], dict[str, RawHolding]] = defaultdict(dict)
    for r in raws:
        groups[(r.portfolio, r.on_date)][r.key] = r

    snapshots: list[_Snapshot] = []
    rate_gaps: list[RateGap] = []
    unclassified: list[str] = []
    for (portfolio, on_date), grp in groups.items():
        valued = value_holdings(
            list(grp.values()), commodities=commodities, rate_source=rate_source
        )
        snapshots.append(
            _Snapshot(
                portfolio, on_date, valued.gross_long_gbp,
                valued.net_cash_gbp, valued.net_worth_gbp,
                valued.missing_prices,
            )
        )
        rate_gaps.extend(valued.rate_gaps)
        unclassified.extend(valued.unclassified)

    by_portfolio: dict[str, list[_Snapshot]] = defaultdict(list)
    for s in snapshots:
        by_portfolio[s.portfolio].append(s)
    for lst in by_portfolio.values():
        lst.sort(key=lambda s: s.on_date)

    points: list[NetWorthPoint] = []
    prev_nw: Decimal | None = None
    snapshot_dates = {s.on_date for s in snapshots}
    if monthly and snapshot_dates:
        all_dates = _month_start_grid(min(snapshot_dates), max(snapshot_dates))
    else:
        all_dates = sorted(snapshot_dates)
    for d in all_dates:
        gross = net_cash = net_worth = _ZERO
        contributing = 0
        for lst in by_portfolio.values():
            chosen = as_of(lst, d, key=lambda s: s.on_date)
            if chosen is not None:
                gross += chosen.gross_long_gbp
                net_cash += chosen.net_cash_gbp
                net_worth += chosen.net_worth_gbp
                contributing += 1
        # A leading monthly anchor before any portfolio has data contributes
        # nothing; skip it so the series starts at the first real valuation.
        if contributing == 0:
            continue
        change = None if prev_nw is None else net_worth - prev_nw
        points.append(
            NetWorthPoint(d, gross, net_cash, net_worth, change, contributing)
        )
        prev_nw = net_worth

    # Unvaluable holdings are reported only for the *latest* point — the
    # snapshot each portfolio contributes as-of the final date — so the
    # warning names holdings currently held but unvaluable, not ones a
    # long-superseded historical statement happened to mis-price.
    missing_prices: list[str] = []
    if all_dates:
        latest = all_dates[-1]
        for lst in by_portfolio.values():
            chosen = as_of(lst, latest, key=lambda s: s.on_date)
            if chosen is not None:
                missing_prices.extend(chosen.missing_prices)

    return NetWorthTimeline(
        points=tuple(points),
        rate_gaps=tuple(rate_gaps),
        missing_prices=tuple(sorted(set(missing_prices))),
        unclassified=tuple(sorted(set(unclassified))),
    )


# --- rendering --------------------------------------------------------------


def render_markdown(timeline: NetWorthTimeline) -> str:
    points = timeline.points
    lines = ["# Net worth over time", ""]
    if not points:
        lines += ["No statement valuations found.", ""]
        return "\n".join(lines)

    first, last = points[0], points[-1]
    total_change = last.net_worth_gbp - first.net_worth_gbp
    lines += [
        f"From **{first.on_date}** to **{last.on_date}**: net worth "
        f"**{gbp(first.net_worth_gbp)} → {gbp(last.net_worth_gbp)}** "
        f"({'+' if total_change >= _ZERO else ''}{gbp(total_change)}). "
        "Rows are newest first; each uses every portfolio's latest valuation "
        "on or before that date, and Δ is the change since the previous "
        "(older) date. A reporting aid, not advice.",
        "",
        "| Date | Gross long | Net cash | Net worth | Δ net worth |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    # Newest first in the rendered table; the per-row Δ keeps its
    # chronological meaning (change since the previous, older date).
    for p in reversed(points):
        delta = "—" if p.change_gbp is None else (
            f"{'+' if p.change_gbp >= _ZERO else ''}{gbp(p.change_gbp)}"
        )
        lines.append(
            f"| {p.on_date} | {gbp(p.gross_long_gbp)} | {gbp(p.net_cash_gbp)} "
            f"| {gbp(p.net_worth_gbp)} | {delta} |"
        )
    lines.append("")
    lines += [
        "> Caveat: a recognised nil statement (£0.00 account total) retires "
        "its portfolio at the drain date. A portfolio that simply stops "
        "statementing — with no closing nil statement — still keeps "
        "contributing its last snapshot until superseded.",
        "",
    ]

    lines += missing_price_lines(timeline.missing_prices)
    if timeline.rate_gaps:
        lines += rate_gap_lines(
            timeline.rate_gaps,
            title="Some points understate — missing GBP rate",
            intro="A holding in these statement months couldn't be converted "
            "to GBP, so that point's net worth understates. Add the "
            "month/currency to `data/fx/hmrc-monthly-average.csv`:",
        )
    lines += unclassified_lines(timeline.unclassified)

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(timeline: NetWorthTimeline) -> list[list[str]]:
    rows = [[
        "date", "gross_long_gbp", "net_cash_gbp", "net_worth_gbp",
        "change_gbp", "portfolios",
    ]]
    for p in timeline.points:
        rows.append([
            p.on_date.isoformat(), money(p.gross_long_gbp),
            money(p.net_cash_gbp), money(p.net_worth_gbp),
            "" if p.change_gbp is None else money(p.change_gbp),
            str(p.portfolios),
        ])
    return rows
