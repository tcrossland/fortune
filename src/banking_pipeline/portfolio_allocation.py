"""Per-portfolio allocation report.

Where ``concentration`` values the whole book as one combined position,
this breaks the latest valuation down **per portfolio** — each Pictet
account, the Vanguard ISA, and each off-ledger property — so you can see
how every portfolio is allocated and how they compare.

It reuses ``concentration``'s valuation wholesale: each portfolio's latest
statement snapshot is run through ``value_holdings`` independently (so
cash is netted within a portfolio, not across the book), giving a
per-portfolio asset-class + holdings breakdown. A cross-portfolio summary
then shows each portfolio's net worth and its share of the total.

Weights inside a portfolio are a share of *that portfolio's* gross long
holdings (matching ``concentration``); the cross-portfolio shares are of
total net worth. Holdings with no mark / no GBP rate are excluded and
flagged, as in the sibling reports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.property import Property
from banking_pipeline.tax.uk.currency import RateGap
from banking_pipeline.valuation import (
    Holding,
    RawHolding,
    property_raws,
    raw_from_statement,
    value_holdings,
)

_ZERO = Decimal(0)
_CASH = "cash"


@dataclass(frozen=True)
class PortfolioRow:
    label: str  # display label (the account minus its ``Assets:`` prefix)
    as_of: date
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal
    net_worth_gbp: Decimal
    by_class_gbp: tuple[tuple[str, Decimal], ...]  # securities by asset class
    securities: tuple[Holding, ...]  # valued holdings, sorted by value desc


@dataclass(frozen=True)
class PortfolioAllocationReport:
    portfolios: tuple[PortfolioRow, ...]  # sorted by net worth desc
    total_gross_long_gbp: Decimal
    total_net_cash_gbp: Decimal
    total_net_worth_gbp: Decimal
    missing_prices: tuple[str, ...]
    rate_gaps: tuple[RateGap, ...]
    unclassified: tuple[str, ...]


def _label(portfolio: str) -> str:
    """Display label for a portfolio key (``Assets:Pic:K123456001`` →
    ``Pic:K123456001``); property pseudo-portfolios keep their label."""

    return portfolio.removeprefix("Assets:")


def build_report(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    properties: list[Property] | None = None,
) -> PortfolioAllocationReport:
    """Build the per-portfolio allocation report from ``(text, source)`` pairs.

    Only the latest statement per portfolio contributes (older snapshots
    are superseded). ``properties`` are folded in, each as its own
    portfolio at its latest valuation."""

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))
    raws.extend(property_raws(properties or []))
    return _report_from_raw(raws, commodities=commodities, rate_source=rate_source)


def _report_from_raw(
    raws: list[RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> PortfolioAllocationReport:
    latest: dict[str, date] = {}
    for r in raws:
        if r.portfolio not in latest or r.on_date > latest[r.portfolio]:
            latest[r.portfolio] = r.on_date

    by_pf: dict[str, list[RawHolding]] = defaultdict(list)
    for r in raws:
        if r.on_date == latest[r.portfolio]:
            by_pf[r.portfolio].append(r)

    rows: list[PortfolioRow] = []
    missing_prices: list[str] = []
    rate_gaps: list[RateGap] = []
    unclassified: list[str] = []
    for portfolio, prs in by_pf.items():
        rep = value_holdings(prs, commodities=commodities, rate_source=rate_source)
        by_class: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for h in rep.securities:
            by_class[h.asset_class] += h.value_gbp
        rows.append(
            PortfolioRow(
                label=_label(portfolio), as_of=latest[portfolio],
                gross_long_gbp=rep.gross_long_gbp, net_cash_gbp=rep.net_cash_gbp,
                net_worth_gbp=rep.net_worth_gbp,
                by_class_gbp=tuple(sorted(by_class.items(), key=lambda kv: kv[1], reverse=True)),
                securities=rep.securities,
            )
        )
        missing_prices.extend(rep.missing_prices)
        rate_gaps.extend(rep.rate_gaps)
        unclassified.extend(rep.unclassified)

    rows.sort(key=lambda r: r.net_worth_gbp, reverse=True)
    return PortfolioAllocationReport(
        portfolios=tuple(rows),
        total_gross_long_gbp=sum((r.gross_long_gbp for r in rows), _ZERO),
        total_net_cash_gbp=sum((r.net_cash_gbp for r in rows), _ZERO),
        total_net_worth_gbp=sum((r.net_worth_gbp for r in rows), _ZERO),
        missing_prices=tuple(sorted(set(missing_prices))),
        rate_gaps=tuple(rate_gaps),
        unclassified=tuple(sorted(set(unclassified))),
    )


# --- rendering --------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _gbp(value: Decimal) -> str:
    return f"£{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


def _pct(value: Decimal, total: Decimal) -> str:
    if total == _ZERO:
        return "—"
    return f"{(value / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def render_markdown(report: PortfolioAllocationReport) -> str:
    lines = ["# Portfolio allocation", ""]
    if not report.portfolios:
        lines += ["No statement valuations found.", ""]
        return "\n".join(lines)

    total_nw = report.total_net_worth_gbp
    lines += [
        f"Latest valuation per portfolio. Total net worth "
        f"**{_gbp(total_nw)}** across {len(report.portfolios)} portfolio(s). "
        "Within a portfolio, asset-class weights are a share of that "
        "portfolio's gross long holdings; the table below shares each "
        "portfolio against total net worth. A reporting aid, not advice.",
        "",
        "## By portfolio",
        "",
        "| Portfolio | As at | Gross long | Net cash | Net worth | % of total |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in report.portfolios:
        lines.append(
            f"| {r.label} | {r.as_of} | {_gbp(r.gross_long_gbp)} "
            f"| {_gbp(r.net_cash_gbp)} | {_gbp(r.net_worth_gbp)} "
            f"| {_pct(r.net_worth_gbp, total_nw)} |"
        )
    lines += [
        f"| **Total** | — | {_gbp(report.total_gross_long_gbp)} "
        f"| {_gbp(report.total_net_cash_gbp)} | {_gbp(total_nw)} | 100.0% |",
        "",
    ]

    for r in report.portfolios:
        lines += [f"## {r.label} ({r.as_of})", ""]
        if r.net_cash_gbp < _ZERO:
            lines += [
                f"> Leveraged: net cash {_gbp(r.net_cash_gbp)} (a margin / "
                "Lombard loan).",
                "",
            ]
        lines += [
            "### Asset class",
            "",
            "| Asset class | Value | Weight |",
            "| --- | ---: | ---: |",
        ]
        for cls, value in r.by_class_gbp:
            lines.append(f"| {cls} | {_gbp(value)} | {_pct(value, r.gross_long_gbp)} |")
        if r.net_cash_gbp != _ZERO:
            lines.append(
                f"| {_CASH} (net) | {_gbp(r.net_cash_gbp)} "
                f"| {_pct(r.net_cash_gbp, r.gross_long_gbp)} |"
            )
        lines.append("")
        if r.securities:
            lines += [
                "### Holdings",
                "",
                "| Holding | Asset class | Value | Weight |",
                "| --- | --- | ---: | ---: |",
            ]
            for h in r.securities:
                lines.append(
                    f"| {h.name} ({h.key}) | {h.asset_class} | {_gbp(h.value_gbp)} "
                    f"| {_pct(h.value_gbp, r.gross_long_gbp)} |"
                )
            lines.append("")

    if report.missing_prices:
        lines += [
            "## ⚠️ Unvaluable holdings (no statement mark)",
            "",
            "Held but excluded — the latest statement carried no price:",
            "",
        ]
        lines += [f"- {k}" for k in report.missing_prices]
        lines.append("")
    if report.rate_gaps:
        uniq = sorted(set(report.rate_gaps), key=lambda g: (g.month, g.currency, g.isin))
        lines += [
            "## ⚠️ Excluded — missing GBP rate",
            "",
            "Valued in a non-GBP currency with no rate, so excluded. Add the "
            "month/currency to `data/fx/hmrc-monthly-average.csv`:",
            "",
        ]
        lines += [f"- {g.currency} {g.month} ({g.isin})" for g in uniq]
        lines.append("")
    if report.unclassified:
        lines += [
            "## ⚠️ Unclassified holdings (no metadata)",
            "",
            "Counted by value but bucketed `unknown` — add them to "
            "`data/commodities.toml` for accurate breakdowns:",
            "",
        ]
        lines += [f"- {k}" for k in report.unclassified]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: PortfolioAllocationReport) -> list[list[str]]:
    """Long-format rows: one per (portfolio, asset class), plus a cash row
    per portfolio. ``weight_pct`` is a share of that portfolio's gross long."""

    out = [[
        "portfolio", "as_of", "asset_class", "value_gbp", "weight_pct",
        "portfolio_net_worth_gbp",
    ]]

    def _weight(value: Decimal, total: Decimal) -> str:
        if total == _ZERO:
            return ""
        return str((value / total * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

    for r in report.portfolios:
        for cls, value in r.by_class_gbp:
            out.append([
                r.label, r.as_of.isoformat(), cls, _money(value),
                _weight(value, r.gross_long_gbp), _money(r.net_worth_gbp),
            ])
        out.append([
            r.label, r.as_of.isoformat(), _CASH, _money(r.net_cash_gbp),
            _weight(r.net_cash_gbp, r.gross_long_gbp), _money(r.net_worth_gbp),
        ])
    return out
