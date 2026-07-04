"""Asset-allocation-over-time report.

Tracks the asset-class mix (equity / bond / property / … plus net cash)
across the statement history, so allocation *drift* is visible — what the
point-in-time ``concentration`` report shows for the latest snapshot,
followed through every statement date.

It composes the two existing valuation reports rather than re-deriving
anything: each statement snapshot is valued exactly as ``concentration``
does (``value_holdings`` — securities at ``qty × mark``, cash netted by
currency, converted to GBP at the snapshot date), and the snapshots are
stitched into a timeline with the same as-of forward-fill ``net-worth``
uses (each portfolio contributes its latest valuation on or before each
date, same-date duplicates deduped per commodity).

Weights are a **share of gross long holdings**, matching ``concentration``:
the security asset classes sum to ~100%, and net cash (negative under a
margin / Lombard loan) is reported as its own line so a leveraged book
doesn't distort the mix. Holdings with no statement mark or no GBP rate
are excluded and flagged, exactly as the sibling reports do.
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
    pct,
    rate_gap_lines,
    unclassified_lines,
    weight,
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
_CASH = "cash"

# Asset classes are rendered in this order, then any others alphabetically,
# with ``unknown`` always last so an un-classified holding doesn't lead.
# The first five mirror ``CommodityMetadata.AssetClass``; ``property`` is
# the off-ledger residential class injected by ``property_raws``.
_CLASS_ORDER = (
    "equity-etf", "equity-fund", "bond", "money-market", "property", "other"
)
_UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Snapshot:
    portfolio: str
    on_date: date
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal
    by_class: tuple[tuple[str, Decimal], ...]  # securities aggregated by class
    missing_prices: tuple[str, ...]  # unvaluable holdings in this snapshot


@dataclass(frozen=True)
class AllocationPoint:
    on_date: date
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal
    net_worth_gbp: Decimal
    by_class_gbp: tuple[tuple[str, Decimal], ...]  # security classes, ordered
    portfolios: int


@dataclass(frozen=True)
class AllocationTimeline:
    points: tuple[AllocationPoint, ...]
    asset_classes: tuple[str, ...]  # ordered union of security classes seen
    rate_gaps: tuple[RateGap, ...]
    missing_prices: tuple[str, ...]
    unclassified: tuple[str, ...]  # valued but no commodities.toml metadata


def _order_classes(classes: set[str]) -> list[str]:
    """Preferred order first, then the rest alphabetically, ``unknown`` last."""

    rest = sorted(c for c in classes if c not in _CLASS_ORDER and c != _UNKNOWN)
    ordered = [c for c in _CLASS_ORDER if c in classes] + rest
    if _UNKNOWN in classes:
        ordered.append(_UNKNOWN)
    return ordered


def build_timeline(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    properties: list[Property] | None = None,
) -> AllocationTimeline:
    """Build the allocation timeline from ``(text, source-name)`` pairs.

    ``properties`` (off-ledger residential property) each contribute a
    snapshot per valuation date as a pseudo-portfolio, joining via the same
    forward-fill (asset class ``property``)."""

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))
        drained = drained_portfolio_snapshot(text)
        if drained is not None:
            raws.append(drained)
    raws.extend(property_raws(properties or []))
    return _timeline_from_raw(raws, commodities=commodities, rate_source=rate_source)


def _timeline_from_raw(
    raws: list[RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> AllocationTimeline:
    # One snapshot per (portfolio, statement date). Dedupe by commodity
    # within a snapshot: a monthly and a quarterly statement can share an
    # "as at" date, and counting the same holding twice would double it.
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
        by_class: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for h in valued.securities:
            by_class[h.asset_class] += h.value_gbp
        snapshots.append(
            _Snapshot(
                portfolio, on_date, valued.gross_long_gbp, valued.net_cash_gbp,
                tuple(by_class.items()), valued.missing_prices,
            )
        )
        rate_gaps.extend(valued.rate_gaps)
        unclassified.extend(valued.unclassified)

    by_portfolio: dict[str, list[_Snapshot]] = defaultdict(list)
    for s in snapshots:
        by_portfolio[s.portfolio].append(s)
    for lst in by_portfolio.values():
        lst.sort(key=lambda s: s.on_date)

    seen_classes: set[str] = set()
    points: list[AllocationPoint] = []
    all_dates = sorted({s.on_date for s in snapshots})
    for d in all_dates:
        gross = net_cash = _ZERO
        agg: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        contributing = 0
        for lst in by_portfolio.values():
            chosen = as_of(lst, d, key=lambda s: s.on_date)
            if chosen is None:
                continue
            gross += chosen.gross_long_gbp
            net_cash += chosen.net_cash_gbp
            for cls, v in chosen.by_class:
                agg[cls] += v
            contributing += 1
        seen_classes |= set(agg)
        ordered = _order_classes(set(agg))
        points.append(
            AllocationPoint(
                on_date=d, gross_long_gbp=gross, net_cash_gbp=net_cash,
                net_worth_gbp=gross + net_cash,
                by_class_gbp=tuple((c, agg[c]) for c in ordered),
                portfolios=contributing,
            )
        )

    # Unvaluable holdings are reported only for the *latest* point (the
    # snapshot each portfolio contributes as-of the final date), so the
    # warning names currently-held unvaluable holdings, not ones a
    # long-superseded historical statement happened to mis-price.
    missing_prices: list[str] = []
    if all_dates:
        latest = all_dates[-1]
        for lst in by_portfolio.values():
            chosen = as_of(lst, latest, key=lambda s: s.on_date)
            if chosen is not None:
                missing_prices.extend(chosen.missing_prices)

    return AllocationTimeline(
        points=tuple(points),
        asset_classes=tuple(_order_classes(seen_classes)),
        rate_gaps=tuple(rate_gaps),
        missing_prices=tuple(sorted(set(missing_prices))),
        unclassified=tuple(sorted(set(unclassified))),
    )


# --- rendering --------------------------------------------------------------


def render_markdown(timeline: AllocationTimeline) -> str:
    points = timeline.points
    lines = ["# Asset allocation over time", ""]
    if not points:
        lines += ["No statement valuations found.", ""]
        return "\n".join(lines)

    classes = timeline.asset_classes
    lines += [
        f"From **{points[0].on_date}** to **{points[-1].on_date}**. Each cell "
        "is a share of that date's gross long holdings (the security classes "
        "sum to ~100%); net cash is shown separately (negative = a margin / "
        "Lombard loan). Values are statement marks converted to GBP — a "
        "reporting aid, not advice.",
        "",
        "| Date | " + " | ".join(c.title() for c in classes)
        + " | Net cash | Net worth |",
        "| --- | " + " | ".join("---:" for _ in classes) + " | ---: | ---: |",
    ]
    for p in points:
        by_class_map = dict(p.by_class_gbp)
        cells = " | ".join(pct(by_class_map.get(c, _ZERO), p.gross_long_gbp) for c in classes)
        lines.append(
            f"| {p.on_date} | {cells} | {pct(p.net_cash_gbp, p.gross_long_gbp)} "
            f"| {gbp(p.net_worth_gbp)} |"
        )
    lines.append("")

    # Latest absolute breakdown, so the % table is anchored to real figures.
    last = points[-1]
    lines += [
        f"## Latest breakdown ({last.on_date})",
        "",
        "| Asset class | Value | Weight |",
        "| --- | ---: | ---: |",
    ]
    for cls, value in last.by_class_gbp:
        lines.append(f"| {cls.title()} | {gbp(value)} | {pct(value, last.gross_long_gbp)} |")
    lines.append(
        f"| {_CASH.title()} (net) | {gbp(last.net_cash_gbp)} "
        f"| {pct(last.net_cash_gbp, last.gross_long_gbp)} |"
    )
    lines.append("")
    lines += [
        "> Caveat: a recognised nil statement (£0.00 account total) retires "
        "its portfolio at the drain date. A portfolio that simply stops "
        "statementing — with no closing nil statement — still keeps "
        "contributing its last snapshot to the mix until superseded.",
        "",
    ]

    lines += missing_price_lines(timeline.missing_prices)
    if timeline.rate_gaps:
        lines += rate_gap_lines(
            timeline.rate_gaps,
            title="Some points understate — missing GBP rate",
            intro="A holding in these statement months couldn't be converted "
            "to GBP, so that point's allocation understates. Add the "
            "month/currency to `data/fx/hmrc-monthly-average.csv`:",
        )
    lines += unclassified_lines(timeline.unclassified)

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(timeline: AllocationTimeline) -> list[list[str]]:
    """Long-format rows (one per date × asset class, plus a cash row per
    date) so the timeline pivots cleanly in a spreadsheet."""

    out = [["date", "asset_class", "value_gbp", "weight_pct", "gross_long_gbp", "net_worth_gbp"]]

    for p in timeline.points:
        for cls, value in p.by_class_gbp:
            out.append([
                p.on_date.isoformat(), cls, money(value),
                weight(value, p.gross_long_gbp),
                money(p.gross_long_gbp), money(p.net_worth_gbp),
            ])
        out.append([
            p.on_date.isoformat(), _CASH, money(p.net_cash_gbp),
            weight(p.net_cash_gbp, p.gross_long_gbp),
            money(p.gross_long_gbp), money(p.net_worth_gbp),
        ])
    return out
