"""Income-by-source report.

Aggregates investment **income** — dividends and interest *received* —
from the JSONL transaction sidecars, grouped by period (UK tax year or
calendar year) and by source (the paying holding, or the cash account
for interest), valued in GBP.

This is a financial report, not a tax one, so it differs from the
``tax-report`` pipeline in two deliberate ways:

* **ISA income is included**, flagged via the ``wrapper`` column rather
  than dropped at a tax-exempt choke point — an ISA's dividends and
  interest are genuine income even though they're tax-free.
* **UK and foreign income both count.** SA106 excludes GB-domiciled
  securities (they belong on SA100); here every paying holding is a
  source regardless of domicile.

What counts as income:

* **Dividends** — any :data:`DIVIDEND_TYPES` advice with an ISIN. A
  >60%-interest-bearing offshore fund (the ``distributions_as_interest``
  metadata flag) has its distribution reclassified to interest, exactly
  as SA106 does, so a "bond fund" doesn't read as a dividend payer.
* **Interest received** — a Pictet current-account interest payment
  with a *positive* cash amount (a credit-balance payment to the user;
  the negative ones are overdraft interest the user *pays*, an expense,
  and are excluded), and a Vanguard ISA "Cash Account Interest" credit.

Amounts are converted to GBP with the same resolution order as the tax
pipeline (per-transaction ``gbp_rate`` first, then the rate source); an
amount that can't be converted is dropped and recorded as a
:class:`RateGap` so the report can warn rather than silently understate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.tax.uk.currency import RateGap, to_gbp
from banking_pipeline.tax.uk.tax_year import date_to_tax_year
from banking_pipeline.writer.builders.dividend import DIVIDEND_TYPES
from banking_pipeline.writer.builders.interest import INTEREST_TYPES

_ZERO = Decimal(0)

PeriodMode = Literal["tax-year", "calendar"]
IncomeKind = Literal["dividend", "interest"]

# Identifiers for the non-dividend (cash-interest) income sources. They
# carry no ISIN, so a stable synthetic key groups them per currency.
_CASH_INTEREST = ("cash-interest", "Cash account interest")
_ISA_CASH_INTEREST = ("isa-cash-interest", "ISA cash interest")


@dataclass(frozen=True)
class IncomeRow:
    period: str  # tax-year label ("2025-26") or calendar year ("2025")
    kind: IncomeKind
    source_key: str  # ISIN for dividends, a synthetic key for cash interest
    source_name: str
    currency: str
    wrapper: str | None  # "isa" for tax-free wrapper income, else None
    gross_gbp: Decimal
    wht_gbp: Decimal
    net_gbp: Decimal
    count: int


@dataclass
class IncomeReport:
    period_mode: PeriodMode
    rows: list[IncomeRow]
    missing_rates: list[RateGap] = field(default_factory=list)


def _income_date(tx: Transaction) -> date:
    """The date the income arises — booking/payment date, falling back to
    the settlement then trade date (mirrors SA106)."""

    return tx.booking_date or tx.settlement_date or tx.trade_date


def _classify(
    tx: Transaction, commodities: dict[str, CommodityMetadata]
) -> tuple[IncomeKind, str, str] | None:
    """Return ``(kind, source_key, source_name)`` if ``tx`` is income, else
    ``None``. See the module docstring for what qualifies."""

    doc = tx.document_type
    if doc in DIVIDEND_TYPES and tx.isin:
        meta = commodities.get(tx.isin)
        # The UK "bond fund" rule: a >60%-interest-bearing offshore fund's
        # distribution is interest, not a dividend.
        kind: IncomeKind = (
            "interest" if (meta is not None and meta.distributions_as_interest)
            else "dividend"
        )
        return kind, tx.isin, (meta.name if meta is not None else tx.isin)
    # Credit-balance current-account interest paid *to* the user. The
    # negative ones are overdraft interest the user pays (an expense), not
    # income, so the positive-amount guard excludes them.
    if doc in INTEREST_TYPES and tx.amount > _ZERO:
        return "interest", *_CASH_INTEREST
    # Vanguard ISA cash interest — the regular statement also carries
    # deposits (contributions to equity), so filter on the narration the
    # builder keys on.
    if (
        doc == DocumentType.VANGUARD_REGULAR_STATEMENT
        and "Interest" in tx.narration
        and tx.amount > _ZERO
    ):
        return "interest", *_ISA_CASH_INTEREST
    return None


def _period(on_date: date, mode: PeriodMode) -> str:
    return date_to_tax_year(on_date) if mode == "tax-year" else str(on_date.year)


@dataclass
class _Acc:
    gross: Decimal = _ZERO
    wht: Decimal = _ZERO
    net: Decimal = _ZERO
    count: int = 0
    name: str = ""


def compute_income(
    transactions: list[Transaction],
    *,
    period: PeriodMode,
    commodities: dict[str, CommodityMetadata],
    source: GbpRateSource | None = None,
) -> IncomeReport:
    """Aggregate income from ``transactions`` into GBP rows.

    Each qualifying transaction is grouped by
    ``(period, kind, source, currency, wrapper)`` and summed. Amounts
    that can't be converted to GBP are dropped and their gap recorded on
    :attr:`IncomeReport.missing_rates`.
    """

    groups: dict[tuple[str, IncomeKind, str, str, str | None], _Acc] = defaultdict(_Acc)
    gaps: set[RateGap] = set()

    for tx in transactions:
        classified = _classify(tx, commodities)
        if classified is None:
            continue
        kind, source_key, source_name = classified

        on = _income_date(tx)
        gross_native = tx.gross_income if tx.gross_income is not None else tx.amount
        wht_native = tx.withholding_tax if tx.withholding_tax is not None else _ZERO
        gross = to_gbp(
            gross_native, currency=tx.currency, on_date=on,
            gbp_rate=tx.gbp_rate, source=source,
        )
        wht = to_gbp(
            wht_native, currency=tx.currency, on_date=on,
            gbp_rate=tx.gbp_rate, source=source,
        )
        net = to_gbp(
            tx.amount, currency=tx.currency, on_date=on,
            gbp_rate=tx.gbp_rate, source=source,
        )
        if gross is None or wht is None or net is None:
            gaps.add(RateGap.at(source_key, tx.currency, on))
            continue

        acc = groups[(_period(on, period), kind, source_key, tx.currency, tx.account_wrapper)]
        acc.gross += gross
        acc.wht += wht
        acc.net += net
        acc.count += 1
        acc.name = source_name

    rows = [
        IncomeRow(
            period=period_label, kind=kind, source_key=source_key,
            source_name=acc.name, currency=currency, wrapper=wrapper,
            gross_gbp=acc.gross, wht_gbp=acc.wht, net_gbp=acc.net, count=acc.count,
        )
        for (period_label, kind, source_key, currency, wrapper), acc in groups.items()
    ]
    # Period first (chronological — both label forms sort lexically), then
    # dividends before interest, then by source name for a stable table.
    rows.sort(key=lambda r: (r.period, r.kind, r.source_name, r.currency))
    return IncomeReport(
        period_mode=period,
        rows=rows,
        missing_rates=sorted(gaps, key=lambda g: (g.month, g.currency, g.isin)),
    )


# --- rendering --------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _gbp(value: Decimal) -> str:
    return f"£{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


def _period_noun(mode: PeriodMode) -> str:
    return "tax year" if mode == "tax-year" else "calendar year"


def render_markdown(report: IncomeReport) -> str:
    rows = report.rows
    lines = ["# Income by source", ""]
    if not rows:
        lines += ["No dividend or interest income found.", ""]
        return "\n".join(lines)

    periods = sorted({r.period for r in rows})
    grand_net = sum((r.net_gbp for r in rows), _ZERO)
    tax_free = sum((r.net_gbp for r in rows if r.wrapper is not None), _ZERO)
    lines += [
        f"Dividend and interest income by {_period_noun(report.period_mode)} and "
        f"source, valued in GBP. Total net income **{_gbp(grand_net)}** across "
        f"{len(periods)} {_period_noun(report.period_mode)}(s)"
        + (f", of which **{_gbp(tax_free)}** is tax-free (ISA)." if tax_free else ".")
        + " A reporting aid, not advice.",
        "",
        "## Totals by period",
        "",
        "| Period | Dividends (net) | Interest (net) | Total (net) | of which tax-free |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for p in periods:
        prows = [r for r in rows if r.period == p]
        div = sum((r.net_gbp for r in prows if r.kind == "dividend"), _ZERO)
        interest = sum((r.net_gbp for r in prows if r.kind == "interest"), _ZERO)
        free = sum((r.net_gbp for r in prows if r.wrapper is not None), _ZERO)
        lines.append(
            f"| {p} | {_gbp(div)} | {_gbp(interest)} | {_gbp(div + interest)} "
            f"| {_gbp(free) if free else '—'} |"
        )
    lines.append("")

    for p in periods:
        prows = [r for r in rows if r.period == p]
        lines += [
            f"## {p}",
            "",
            "| Source | Type | Ccy | Gross | WHT | Net | Wrapper | Payments |",
            "| --- | --- | --- | ---: | ---: | ---: | --- | ---: |",
        ]
        for r in prows:
            lines.append(
                f"| {r.source_name} | {r.kind} | {r.currency} | {_gbp(r.gross_gbp)} "
                f"| {_gbp(r.wht_gbp)} | {_gbp(r.net_gbp)} | {r.wrapper or '—'} "
                f"| {r.count} |"
            )
        lines.append("")

    if report.missing_rates:
        lines += [
            "## ⚠️ Some income excluded — missing GBP rate",
            "",
            "These (source, month) amounts couldn't be converted to GBP and "
            "are omitted from the totals. Add the month/currency to "
            "`data/fx/hmrc-monthly-average.csv`:",
            "",
        ]
        lines += [f"- {g.currency} {g.month} ({g.isin})" for g in report.missing_rates]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: IncomeReport) -> list[list[str]]:
    out = [[
        "period", "kind", "source_key", "source_name", "currency", "wrapper",
        "gross_gbp", "wht_gbp", "net_gbp", "payments",
    ]]
    for r in report.rows:
        out.append([
            r.period, r.kind, r.source_key, r.source_name, r.currency,
            r.wrapper or "", _money(r.gross_gbp), _money(r.wht_gbp),
            _money(r.net_gbp), str(r.count),
        ])
    return out
