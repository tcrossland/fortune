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

Caveat: a wound-down portfolio keeps contributing its last *parsed*
snapshot until a newer one supersedes it — an empty valuation table parses
to no holdings and so doesn't refresh the forward-fill. For this single
user that only affects the small Vanguard ISA, but it means a closed
account can linger until its next (empty) statement is replaced by data.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.concentration import (
    _raw_from_statement,
    _RawHolding,
    _value_holdings,
)
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.tax.uk.currency import RateGap

_ZERO = Decimal(0)


@dataclass(frozen=True)
class _Snapshot:
    portfolio: str
    on_date: date
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal
    net_worth_gbp: Decimal


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


def build_timeline(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> NetWorthTimeline:
    """Build the net-worth timeline from ``(text, source-name)`` pairs."""

    raws: list[_RawHolding] = []
    for text, source in statements:
        raws.extend(_raw_from_statement(text, source))
    return _timeline_from_raw(raws, commodities=commodities, rate_source=rate_source)


def _timeline_from_raw(
    raws: list[_RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> NetWorthTimeline:
    # One snapshot per (portfolio, statement date), valued at that date.
    # Dedupe by commodity within a snapshot: two statements can share an
    # "as at" date (a monthly and a quarterly/annual both dated to the same
    # period end), and counting the same holding from both would double the
    # valuation.
    groups: dict[tuple[str, date], dict[str, _RawHolding]] = defaultdict(dict)
    for r in raws:
        groups[(r.portfolio, r.on_date)][r.key] = r

    snapshots: list[_Snapshot] = []
    rate_gaps: list[RateGap] = []
    missing_prices: list[str] = []
    for (portfolio, on_date), grp in groups.items():
        valued = _value_holdings(
            list(grp.values()), commodities=commodities, rate_source=rate_source
        )
        snapshots.append(
            _Snapshot(
                portfolio, on_date, valued.gross_long_gbp,
                valued.net_cash_gbp, valued.net_worth_gbp,
            )
        )
        rate_gaps.extend(valued.rate_gaps)
        missing_prices.extend(valued.missing_prices)

    by_portfolio: dict[str, list[_Snapshot]] = defaultdict(list)
    for s in snapshots:
        by_portfolio[s.portfolio].append(s)
    for lst in by_portfolio.values():
        lst.sort(key=lambda s: s.on_date)

    points: list[NetWorthPoint] = []
    prev_nw: Decimal | None = None
    for d in sorted({s.on_date for s in snapshots}):
        gross = net_cash = net_worth = _ZERO
        contributing = 0
        for lst in by_portfolio.values():
            chosen = _as_of(lst, d)
            if chosen is not None:
                gross += chosen.gross_long_gbp
                net_cash += chosen.net_cash_gbp
                net_worth += chosen.net_worth_gbp
                contributing += 1
        change = None if prev_nw is None else net_worth - prev_nw
        points.append(
            NetWorthPoint(d, gross, net_cash, net_worth, change, contributing)
        )
        prev_nw = net_worth

    return NetWorthTimeline(
        points=tuple(points),
        rate_gaps=tuple(rate_gaps),
        missing_prices=tuple(sorted(set(missing_prices))),
    )


def _as_of(snapshots: list[_Snapshot], on_date: date) -> _Snapshot | None:
    """The latest snapshot on or before ``on_date`` (list is date-sorted)."""

    chosen: _Snapshot | None = None
    for s in snapshots:
        if s.on_date <= on_date:
            chosen = s
        else:
            break
    return chosen


# --- rendering --------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _gbp(value: Decimal) -> str:
    return f"£{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


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
        f"**{_gbp(first.net_worth_gbp)} → {_gbp(last.net_worth_gbp)}** "
        f"({'+' if total_change >= _ZERO else ''}{_gbp(total_change)}). "
        "Each row uses every portfolio's latest valuation on or before that "
        "date. A reporting aid, not advice.",
        "",
        "| Date | Gross long | Net cash | Net worth | Δ net worth |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for p in points:
        delta = "—" if p.change_gbp is None else (
            f"{'+' if p.change_gbp >= _ZERO else ''}{_gbp(p.change_gbp)}"
        )
        lines.append(
            f"| {p.on_date} | {_gbp(p.gross_long_gbp)} | {_gbp(p.net_cash_gbp)} "
            f"| {_gbp(p.net_worth_gbp)} | {delta} |"
        )
    lines.append("")

    if timeline.rate_gaps:
        uniq = sorted(set(timeline.rate_gaps), key=lambda g: (g.month, g.currency, g.isin))
        lines += [
            "## ⚠️ Some points understate — missing GBP rate",
            "",
            "A holding in these statement months couldn't be converted to "
            "GBP, so that point's net worth understates. Add the "
            "month/currency to `data/fx/hmrc-monthly-average.csv`:",
            "",
        ]
        lines += [f"- {g.currency} {g.month} ({g.isin})" for g in uniq]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(timeline: NetWorthTimeline) -> list[list[str]]:
    rows = [[
        "date", "gross_long_gbp", "net_cash_gbp", "net_worth_gbp",
        "change_gbp", "portfolios",
    ]]
    for p in timeline.points:
        rows.append([
            p.on_date.isoformat(), _money(p.gross_long_gbp),
            _money(p.net_cash_gbp), _money(p.net_worth_gbp),
            "" if p.change_gbp is None else _money(p.change_gbp),
            str(p.portfolios),
        ])
    return rows
