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
) -> NetWorthTimeline:
    """Build the net-worth timeline from ``(text, source-name)`` pairs.

    ``properties`` (off-ledger residential property) each become a
    pseudo-portfolio contributing a snapshot per valuation date, so they
    join the timeline via the same as-of forward-fill."""

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))
    raws.extend(property_raws(properties or []))
    return _timeline_from_raw(raws, commodities=commodities, rate_source=rate_source)


def _timeline_from_raw(
    raws: list[RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> NetWorthTimeline:
    # One snapshot per (portfolio, statement date), valued at that date.
    # Dedupe by commodity within a snapshot: two statements can share an
    # "as at" date (a monthly and a quarterly/annual both dated to the same
    # period end), and counting the same holding from both would double the
    # valuation.
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
    all_dates = sorted({s.on_date for s in snapshots})
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
        "> Caveat: a wound-down portfolio keeps contributing its last "
        "non-empty snapshot until a newer statement supersedes it (an empty "
        "valuation doesn't refresh the as-of fill), so a closed account can "
        "linger and overstate a later point.",
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
