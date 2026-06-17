"""Portfolio concentration / exposure report.

Reads the *latest* statement valuation per portfolio — the Pictet monthly
statement's portfolio-valuation page and the Vanguard ISA regular
statement's "Your ISA investments at …" snapshot, the same valuations
``balances`` / ``prices`` already parse — values every holding in GBP,
and breaks the total down five ways (by holding, asset class, quotation
currency, domicile, and issuer) so over-weight positions are visible.

It is a *reporting aid*: values are the statement marks (quantity × the
statement's per-unit price) converted to GBP at the configured rate
source. A holding with no statement mark, or one that can't be converted
to GBP, is excluded from the figures and surfaced as a warning rather
than silently understating a weight.

Holdings key on the statement's own commodity identifier — an ISIN for
Pictet, a ticker for the Vanguard ISA. ``commodities.toml`` is ISIN-keyed,
so Vanguard tickers carry no metadata and land in an ``unknown`` asset
class / domicile / issuer bucket (flagged). The "by issuer" breakdown is
single-provider / counterparty exposure (fund house) — distinct from "by
domicile", which is kept because UK-tax situs and reporting status key off
domicile, not issuer. Issuer comes from the ``issuer`` metadata field, or
is inferred from the fund name when that's unset (see
:func:`banking_pipeline.commodities_metadata.infer_issuer`).
"""

from __future__ import annotations

from collections import defaultdict
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
from banking_pipeline.valuation import (
    Holding,
    RawHolding,
    ValuationResult,
    property_raws,
    raw_from_statement,
    value_holdings,
)

_ZERO = Decimal(0)


def build_report(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    properties: list[Property] | None = None,
) -> ValuationResult:
    """Build the concentration report from ``(text, source-name)`` pairs.

    Only the latest statement per portfolio contributes (older snapshots
    are superseded), so passing a whole directory of statements yields the
    current position. Holdings are valued in GBP and sorted by value.
    ``properties`` (off-ledger residential property) are folded in as
    holdings at their latest valuation.
    """

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))
    raws.extend(property_raws(properties or []))
    return _build_from_raw(raws, commodities=commodities, rate_source=rate_source)


def _build_from_raw(
    raws: list[RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> ValuationResult:
    """Value + aggregate raw holdings into a report (the testable core).

    Only the latest statement per portfolio contributes — older snapshots
    are superseded — so the result is the current position.
    """

    latest: dict[str, date] = {}
    for r in raws:
        if r.portfolio not in latest or r.on_date > latest[r.portfolio]:
            latest[r.portfolio] = r.on_date
    current = [r for r in raws if r.on_date == latest[r.portfolio]]
    return value_holdings(current, commodities=commodities, rate_source=rate_source)



# --- rendering --------------------------------------------------------------


def _aggregate(holdings: tuple[Holding, ...], attr: str) -> list[tuple[str, Decimal]]:
    agg: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for h in holdings:
        agg[getattr(h, attr)] += h.value_gbp
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)


def _table(title: str, rows: list[tuple[str, Decimal]], total: Decimal) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| | Value | Weight |",
        "| --- | ---: | ---: |",
    ]
    for label, value in rows:
        lines.append(f"| {label} | {gbp(value)} | {pct(value, total)} |")
    lines.append("")
    return lines


def render_markdown(report: ValuationResult) -> str:
    as_of = report.as_of.isoformat() if report.as_of else "—"
    gross = report.gross_long_gbp
    lines = [
        "# Portfolio concentration",
        "",
        f"As at **{as_of}**. Gross long holdings: **{gbp(gross)}**; "
        f"net cash: **{gbp(report.net_cash_gbp)}**; net worth: "
        f"**{gbp(report.net_worth_gbp)}**. Weights below are a share of "
        "gross long holdings (cash / leverage shown separately). Values are "
        "statement marks converted to GBP — a reporting aid, not advice.",
        "",
    ]
    if report.net_cash_gbp < _ZERO:
        lines += [
            f"> The portfolio is **leveraged**: net cash is "
            f"{gbp(report.net_cash_gbp)} (a margin / Lombard loan). Gross "
            f"long holdings of {gbp(gross)} are funded partly by borrowing, "
            "so concentration is measured against the gross long book.",
            "",
        ]
    lines += _table(
        "By holding",
        [(f"{h.name} ({h.key})", h.value_gbp) for h in report.securities],
        gross,
    )
    lines += _table(
        "By asset class", _aggregate(report.securities, "asset_class"), gross
    )
    lines += _table("By currency", _aggregate(report.securities, "currency"), gross)
    lines += _table("By domicile", _aggregate(report.securities, "domicile"), gross)
    # Issuer (fund house) concentration — single-provider / counterparty
    # exposure the domicile view can't surface. Additive: domicile stays,
    # it carries the UK-tax situs / reporting-status read.
    lines += _table("By issuer", _aggregate(report.securities, "issuer"), gross)

    if report.cash:
        lines += [
            "## Cash & leverage",
            "",
            "Net cash per currency (negative = borrowed); a share of gross "
            "long holdings.",
            "",
            "| Currency | Balance | vs. gross long |",
            "| --- | ---: | ---: |",
        ]
        for c in report.cash:
            lines.append(
                f"| {c.currency} | {gbp(c.value_gbp)} | {pct(c.value_gbp, gross)} |"
            )
        lines.append(f"| **Net cash** | {gbp(report.net_cash_gbp)} | "
                     f"{pct(report.net_cash_gbp, gross)} |")
        lines.append("")

    lines += missing_price_lines(report.missing_prices)
    if report.rate_gaps:
        lines += rate_gap_lines(
            report.rate_gaps,
            title="Excluded — missing GBP rate",
            intro="Valued in a non-GBP currency with no rate, so excluded (the "
            "weights above understate). Add the month/currency to "
            "`data/fx/hmrc-monthly-average.csv` and re-run:",
        )
    lines += unclassified_lines(report.unclassified)

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: ValuationResult) -> list[list[str]]:
    """Per-holding rows for the CSV (header first). Securities then cash;
    ``weight_pct`` is a share of gross long holdings (blank for cash)."""

    gross = report.gross_long_gbp

    rows = [[
        "kind", "key", "name", "asset_class", "domicile", "issuer", "currency",
        "quantity", "value_gbp", "weight_pct",
    ]]
    for h in report.securities:
        rows.append([
            "security", h.key, h.name, h.asset_class, h.domicile, h.issuer,
            h.currency, money(h.quantity), money(h.value_gbp),
            weight(h.value_gbp, gross),
        ])
    for c in report.cash:
        rows.append([
            "cash", c.key, c.name, c.asset_class, c.domicile, c.issuer,
            c.currency, money(c.quantity), money(c.value_gbp), "",
        ])
    return rows
