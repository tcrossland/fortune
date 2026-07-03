"""Holdings cost-basis / unrealised-P&L report.

Joins the *latest* statement valuation per portfolio (market value in GBP,
the same marks ``concentration`` uses) with a per-jurisdiction cost basis from
a :class:`~banking_pipeline.basis_lens.BasisLens`, and reports each holding's
unrealised gain/loss. The MVP lens is UK section 104 (GBP); the seam admits a
future EUR/Spanish lens (see :mod:`banking_pipeline.basis_lens`).

Two by-products beyond the headline table:

- a **quantity cross-check** — the statement quantity against the lens's own
  quantity (the section 104 pool), surfacing a missing trade or ingest gap;
- an **unmatched-basis** list — securities the lens still holds that no current
  statement marks (a disposal not yet ingested, or a stale statement).

Cost basis is a UK-tax lens: it is **not** Pictet's EUR/Spanish figures and
will not equal them. A reporting aid, not tax advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.basis_lens import BasisLens, HoldingBasis
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.report_format import (
    gbp,
    missing_price_lines,
    money,
    rate_gap_lines,
)
from banking_pipeline.tax.uk.currency import RateGap
from banking_pipeline.valuation import (
    Holding,
    RawHolding,
    ValuationResult,
    raw_from_statement,
    value_holdings,
)

_ZERO = Decimal(0)
# Symmetric quantity-agreement tolerance for the statement-vs-pool cross-check
# (a fractional-unit rounding guard; numerically matches sa108's over-disposal
# epsilon but is a two-sided |diff| test, not a one-sided shortfall guard).
_QTY_TOL = Decimal("0.0001")


@dataclass(frozen=True)
class HoldingRow:
    """One current holding: statement market value against lens cost basis.

    ``cost_basis_gbp`` / ``unrealised_gbp`` / ``basis_qty`` are ``None`` when
    the lens has no entry for this holding (e.g. a Vanguard ISA line the
    statement keys by ticker, not the ISIN the section 104 lens keys by; or a
    holding with no ledger history)."""

    key: str  # ISIN / ticker
    name: str
    currency: str  # quotation currency
    quantity: Decimal  # statement quantity
    market_value_gbp: Decimal
    cost_basis_gbp: Decimal | None
    unrealised_gbp: Decimal | None
    basis_qty: Decimal | None  # the lens's quantity, for the cross-check


@dataclass(frozen=True)
class QtyDrift:
    """A holding whose statement quantity and lens (section 104 pool)
    quantity disagree beyond tolerance."""

    key: str
    name: str
    statement_qty: Decimal
    pool_qty: Decimal


@dataclass(frozen=True)
class HoldingsReport:
    as_of: date | None
    rows: tuple[HoldingRow, ...]  # sorted by market value desc
    total_market_gbp: Decimal  # every holding
    total_cost_gbp: Decimal  # holdings with a matched basis only
    total_unrealised_gbp: Decimal  # holdings with a matched basis only
    # Statement quantity vs section 104 pool quantity disagreements.
    qty_drifts: tuple[QtyDrift, ...]
    # ISINs the lens still holds (qty > 0) that no current statement marks.
    unmatched_basis: tuple[str, ...]
    # Pass-through valuation warnings.
    missing_prices: tuple[str, ...]
    rate_gaps: tuple[RateGap, ...]


def _latest_per_portfolio(raws: list[RawHolding]) -> list[RawHolding]:
    """Keep only each portfolio's most recent snapshot — the current
    position (older statements are superseded)."""

    latest: dict[str, date] = {}
    for r in raws:
        if r.portfolio not in latest or r.on_date > latest[r.portfolio]:
            latest[r.portfolio] = r.on_date
    return [r for r in raws if r.on_date == latest[r.portfolio]]


@dataclass
class _AggHolding:
    """A security consolidated across portfolios (statement quantity + GBP
    market value summed), before the basis join."""

    name: str
    currency: str
    quantity: Decimal
    value_gbp: Decimal


def _aggregate_by_key(securities: tuple[Holding, ...]) -> dict[str, _AggHolding]:
    """Consolidate valued securities by key, summing quantity and GBP market
    value. The section 104 pool is NIF-level (account-blind, keyed on ISIN),
    so a fund held in more than one mandate must face the pool **once** — an
    un-aggregated per-portfolio row would each see the full pool cost,
    double-count the totals, and false-flag a quantity drift (partial vs full
    pool). Name/currency are taken from the first row (identical per ISIN)."""

    agg: dict[str, _AggHolding] = {}
    for h in securities:
        existing = agg.get(h.key)
        if existing is None:
            agg[h.key] = _AggHolding(h.name, h.currency, h.quantity, h.value_gbp)
        else:
            existing.quantity += h.quantity
            existing.value_gbp += h.value_gbp
    return agg


def join_holdings(
    valuation: ValuationResult, basis: dict[str, HoldingBasis]
) -> HoldingsReport:
    """Join valued securities with per-ISIN cost basis into the report (the
    testable core). Securities are consolidated by key first (see
    :func:`_aggregate_by_key`), then rows are ordered by market value desc.
    Cash and property are ignored — cost basis is a securities concept."""

    aggregated = _aggregate_by_key(valuation.securities)
    keys = sorted(aggregated, key=lambda k: aggregated[k].value_gbp, reverse=True)

    rows: list[HoldingRow] = []
    drifts: list[QtyDrift] = []
    matched: set[str] = set()

    for key in keys:
        agg = aggregated[key]
        hb = basis.get(key)
        if hb is not None:
            matched.add(key)
            # A non-GBP lens carries its own market value (statement-date FX
            # differs); a GBP lens defers to the statement mark.
            market = hb.market_value if hb.market_value is not None else agg.value_gbp
            cost: Decimal | None = hb.cost_amount
            unrealised: Decimal | None = market - hb.cost_amount
            basis_qty: Decimal | None = hb.held_qty
            if abs(agg.quantity - hb.held_qty) > _QTY_TOL:
                drifts.append(
                    QtyDrift(key, agg.name, agg.quantity, hb.held_qty)
                )
        else:
            market = agg.value_gbp
            cost = unrealised = basis_qty = None
        rows.append(
            HoldingRow(
                key=key,
                name=agg.name,
                currency=agg.currency,
                quantity=agg.quantity,
                market_value_gbp=market,
                cost_basis_gbp=cost,
                unrealised_gbp=unrealised,
                basis_qty=basis_qty,
            )
        )

    total_market = sum((r.market_value_gbp for r in rows), _ZERO)
    total_cost = sum(
        (r.cost_basis_gbp for r in rows if r.cost_basis_gbp is not None), _ZERO
    )
    total_unrealised = sum(
        (r.unrealised_gbp for r in rows if r.unrealised_gbp is not None), _ZERO
    )
    unmatched = tuple(sorted(k for k in basis if k not in matched))
    return HoldingsReport(
        as_of=valuation.as_of,
        rows=tuple(rows),
        total_market_gbp=total_market,
        total_cost_gbp=total_cost,
        total_unrealised_gbp=total_unrealised,
        qty_drifts=tuple(drifts),
        unmatched_basis=unmatched,
        missing_prices=valuation.missing_prices,
        rate_gaps=valuation.rate_gaps,
    )


def build_report(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    basis: BasisLens,
) -> HoldingsReport:
    """Build the holdings report from ``(text, source-name)`` statement pairs
    and a cost-basis lens. Only the latest statement per portfolio contributes
    (older snapshots are superseded), so a whole directory yields the current
    position."""

    # The renderer and totals are GBP-only. A non-GBP lens (the reserved ES /
    # EUR-Spanish one) supplies its own market value + cost in its currency;
    # rendering that as £ and summing it into a GBP total would be silently
    # wrong, so it must consciously extend the renderer before it's accepted.
    if basis.currency != "GBP":
        raise NotImplementedError(
            f"holdings report renders GBP only; the {basis.name!r} lens is "
            f"{basis.currency} — a non-GBP lens needs renderer support first"
        )

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))
    valuation = value_holdings(
        _latest_per_portfolio(raws),
        commodities=commodities,
        rate_source=rate_source,
    )
    return join_holdings(valuation, basis.basis_for())


# --- rendering --------------------------------------------------------------


def _amount(value: Decimal | None) -> str:
    return gbp(value) if value is not None else "—"


def render_markdown(report: HoldingsReport) -> str:
    as_of = report.as_of.isoformat() if report.as_of else "—"
    lines = [
        "# Holdings — cost basis & unrealised P&L",
        "",
        f"As at **{as_of}**. Cost basis is **UK section 104 (GBP)** — a UK-tax "
        "lens, not Pictet's EUR/Spanish figures and not equal to them. Market "
        "value is the statement mark converted to GBP. ISA holdings appear but "
        "are UK-tax-exempt (a Spanish-resident lens would tax them) and, keyed "
        "by ticker not ISIN, carry no section 104 cost basis here. Reporting "
        "aid, not advice.",
        "",
        "| Holding | Qty | Market (GBP) | Cost (GBP) | Unrealised (GBP) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in report.rows:
        lines.append(
            f"| {r.name} ({r.key}) | {money(r.quantity)} | "
            f"{gbp(r.market_value_gbp)} | {_amount(r.cost_basis_gbp)} | "
            f"{_amount(r.unrealised_gbp)} |"
        )
    lines += [
        f"| **Total** | | {gbp(report.total_market_gbp)} | "
        f"{gbp(report.total_cost_gbp)} | {gbp(report.total_unrealised_gbp)} |",
        "",
        "Cost and unrealised totals cover only holdings with a matched "
        "section 104 basis; the market-value total covers every holding.",
        "",
    ]

    if report.qty_drifts:
        lines += [
            "## ⚠️ Quantity drift — statement vs section 104 pool",
            "",
            "The statement quantity and the ledger's section 104 pool "
            "disagree — a missing trade confirmation or an ingest gap:",
            "",
            "| Holding | Statement qty | Pool qty |",
            "| --- | ---: | ---: |",
        ]
        for d in report.qty_drifts:
            lines.append(
                f"| {d.name} ({d.key}) | {money(d.statement_qty)} | "
                f"{money(d.pool_qty)} |"
            )
        lines.append("")

    if report.unmatched_basis:
        lines += [
            "## ⚠️ Held per ledger, not on the latest statement",
            "",
            "The section 104 pool still holds these ISINs but no current "
            "statement marks them — a disposal not yet ingested, or a stale "
            "statement:",
            "",
            *[f"- `{k}`" for k in report.unmatched_basis],
            "",
        ]

    lines += missing_price_lines(report.missing_prices)
    if report.rate_gaps:
        lines += rate_gap_lines(
            report.rate_gaps,
            title="Excluded — missing GBP rate",
            intro="Valued in a non-GBP currency with no rate, so excluded from "
            "the market value above. Add the month/currency to "
            "`data/fx/hmrc-monthly-average.csv` and re-run:",
        )

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: HoldingsReport) -> list[list[str]]:
    """Per-holding rows for the CSV (header first). Blank cost / unrealised /
    pool-qty cells mark a holding with no matched section 104 basis."""

    rows = [[
        "key", "name", "currency", "quantity", "market_value_gbp",
        "cost_basis_gbp", "unrealised_gbp", "pool_qty",
    ]]
    for r in report.rows:
        rows.append([
            r.key, r.name, r.currency, money(r.quantity),
            money(r.market_value_gbp),
            money(r.cost_basis_gbp) if r.cost_basis_gbp is not None else "",
            money(r.unrealised_gbp) if r.unrealised_gbp is not None else "",
            money(r.basis_qty) if r.basis_qty is not None else "",
        ])
    return rows
