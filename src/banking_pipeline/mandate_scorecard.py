"""Mandate cost scorecard — step 1: the all-in *explicit* cost block.

Totals what the mandate actually costs each year, from the ledger's
``Expenses:Pic`` accounts (the writer's authoritative cost categorisation):

* **management** — the discretionary-mandate management fee
  (``…:Management``);
* **transaction & custody** — dealing / custody / FX spread / transaction
  tax / wire (``…:Fees`` / ``…:Brokerage`` / ``…:Spread`` / ``…:Tax`` /
  ``…:Wire``);
* **interest** — the Lombard loan interest the client pays (``…:Interest``).

``Expenses:…:Other`` (the catch-all leg of an outgoing payment / transfer)
is **excluded** — it's money moved out, not a cost — and so is foreign
withholding tax (``Expenses:Tax:Withholding`` lives outside ``Expenses:Pic``).

Each posting is converted to GBP at its own date (via the configured
:class:`GbpRateSource`), so a cost that can't be converted is excluded and
flagged rather than silently dropped. Costs are expressed both in £ and as a
share of the year's average **invested assets** (gross long, from the
net-worth timeline) — the fee the active value-add has to clear.

Not included: the underlying **fund TERs** (the in-house funds' expense
ratios), which aren't in the ledger. This is the explicit, ledger-visible
cost only; the implicit fund layer is a separate (estimated) input.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.bean_query import QueryResult, run_query
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.net_worth import NetWorthTimeline
from banking_pipeline.report_format import gbp, money, pct
from banking_pipeline.tax.uk.currency import RateGap, to_gbp

_ZERO = Decimal(0)

# Per-posting Expenses rows, excluding the ``Other`` payment/transfer legs.
_BQL = (
    'SELECT date, account, units(sum(position)) AS amount '
    'WHERE account ~ "Expenses:Pic" AND NOT account ~ ":Other" '
    "GROUP BY date, account ORDER BY date"
)

# Account-leaf category → scorecard bucket.
_MANAGEMENT = "management"
_TRANSACTION = "transaction"
_INTEREST = "interest"
_BUCKET = {
    "Management": _MANAGEMENT,
    "Interest": _INTEREST,
    # everything else (Fees / Brokerage / Spread / Tax / Wire) → transaction
}


@dataclass(frozen=True)
class CostYear:
    year: str
    management_gbp: Decimal
    transaction_gbp: Decimal
    interest_gbp: Decimal
    total_gbp: Decimal
    avg_invested_gbp: Decimal | None  # gross long, average over the year


@dataclass(frozen=True)
class CostReport:
    years: tuple[CostYear, ...]
    rate_gaps: tuple[RateGap, ...]  # cost postings excluded (no GBP rate)


def query_costs(ledger: Path) -> QueryResult:
    """Run the explicit-cost query against ``ledger`` via ``bean-query``."""

    return run_query(ledger, _BQL)


def _is_currency(seg: str) -> bool:
    return len(seg) == 3 and seg.isalpha() and seg.isupper()


def _category(account: str) -> str:
    """The cost category for an ``Expenses:Pic:<portfolio>:<Category>[:<CCY>]``
    account. The writer suffixes most cost legs with a currency segment
    (``…:Management:EUR``), so the category is the segment *before* a trailing
    currency code; an account without one (``…:Other``) uses its own leaf."""

    segs = account.split(":")
    if len(segs) >= 2 and _is_currency(segs[-1]):
        return segs[-2]
    return segs[-1]


def _parse_amount(field: str) -> tuple[Decimal, str] | None:
    parts = field.split()
    if len(parts) != 2:
        return None
    try:
        return Decimal(parts[0].replace(",", "")), parts[1]
    except (ArithmeticError, ValueError):
        return None


def _avg_invested_by_year(timeline: NetWorthTimeline | None) -> dict[str, Decimal]:
    """Average gross-long (invested) value per calendar year from the
    net-worth timeline — the denominator for cost-as-%-of-assets."""

    if timeline is None:
        return {}
    sums: dict[str, list[Decimal]] = defaultdict(list)
    for p in timeline.points:
        sums[str(p.on_date.year)].append(p.gross_long_gbp)
    return {
        y: sum(vals, _ZERO) / Decimal(len(vals)) for y, vals in sums.items() if vals
    }


def build_cost_report(
    result: QueryResult,
    *,
    rate_source: GbpRateSource,
    timeline: NetWorthTimeline | None = None,
) -> CostReport:
    """Aggregate the per-posting cost rows into a per-(calendar-)year report.

    Each row is ``(date, account, "<amount> <ccy>")``. Amounts convert to GBP
    at the posting date; an unconvertible one is excluded and recorded as a
    ``RateGap``. ``timeline`` (optional) supplies the average-invested-assets
    denominator for the cost-as-%-of-assets column.
    """

    buckets: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {_MANAGEMENT: _ZERO, _TRANSACTION: _ZERO, _INTEREST: _ZERO}
    )
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
        bucket = _BUCKET.get(_category(account), _TRANSACTION)
        buckets[str(on_date.year)][bucket] += value

    avg_invested = _avg_invested_by_year(timeline)
    years = [
        CostYear(
            year=y,
            management_gbp=b[_MANAGEMENT],
            transaction_gbp=b[_TRANSACTION],
            interest_gbp=b[_INTEREST],
            total_gbp=b[_MANAGEMENT] + b[_TRANSACTION] + b[_INTEREST],
            avg_invested_gbp=avg_invested.get(y),
        )
        for y, b in sorted(buckets.items())
    ]
    return CostReport(
        years=tuple(years),
        rate_gaps=tuple(gaps),
    )


def render_markdown(report: CostReport) -> str:
    lines = [
        "# Mandate cost scorecard — explicit costs",
        "",
        "All-in **ledger-visible** cost per calendar year (management fee, "
        "transaction & custody, Lombard interest), in GBP and as a share of "
        "that year's average invested assets (gross long). Excludes payment / "
        "transfer legs and the underlying **fund TERs** (not in the ledger — "
        "the implicit in-house layer). A reporting aid, not advice.",
        "",
        "| Year | Management | Transaction & custody | Lombard interest "
        "| Total | % of avg. assets |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    tot_m = tot_t = tot_i = _ZERO
    for c in report.years:
        share = (
            pct(c.total_gbp, c.avg_invested_gbp)
            if c.avg_invested_gbp
            else "—"
        )
        lines.append(
            f"| {c.year} | {gbp(c.management_gbp)} | {gbp(c.transaction_gbp)} "
            f"| {gbp(c.interest_gbp)} | {gbp(c.total_gbp)} | {share} |"
        )
        tot_m += c.management_gbp
        tot_t += c.transaction_gbp
        tot_i += c.interest_gbp
    lines.append(
        f"| **All years** | {gbp(tot_m)} | {gbp(tot_t)} | {gbp(tot_i)} "
        f"| {gbp(tot_m + tot_t + tot_i)} | — |"
    )
    lines.append("")
    if report.rate_gaps:
        lines += [
            "## ⚠️ Costs excluded — missing GBP rate",
            "",
            "These cost postings couldn't be converted to GBP, so the totals "
            "understate; add the month/currency to "
            "`data/fx/hmrc-monthly-average.csv`:",
            "",
            *[
                f"- {g.currency} {g.month} ({g.isin})"
                for g in sorted(set(report.rate_gaps), key=lambda g: (g.month, g.currency))
            ],
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: CostReport) -> list[list[str]]:
    rows: list[list[str]] = [[
        "year", "management_gbp", "transaction_gbp", "interest_gbp",
        "total_gbp", "avg_invested_gbp", "cost_pct",
    ]]
    for c in report.years:
        share = (
            money(c.total_gbp / c.avg_invested_gbp * 100)
            if c.avg_invested_gbp
            else ""
        )
        rows.append([
            c.year, money(c.management_gbp), money(c.transaction_gbp),
            money(c.interest_gbp), money(c.total_gbp),
            money(c.avg_invested_gbp) if c.avg_invested_gbp is not None else "",
            share,
        ])
    return rows
