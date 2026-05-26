"""Portfolio concentration / exposure report.

Reads the *latest* statement valuation per portfolio — the Pictet monthly
statement's portfolio-valuation page and the Vanguard ISA regular
statement's "Your ISA investments at …" snapshot, the same valuations
``balances`` / ``prices`` already parse — values every holding in GBP,
and breaks the total down four ways (by holding, asset class, quotation
currency, and domicile) so over-weight positions are visible.

It is a *reporting aid*: values are the statement marks (quantity × the
statement's per-unit price) converted to GBP at the configured rate
source. A holding with no statement mark, or one that can't be converted
to GBP, is excluded from the figures and surfaced as a warning rather
than silently understating a weight.

Holdings key on the statement's own commodity identifier — an ISIN for
Pictet, a ticker for the Vanguard ISA. ``commodities.toml`` is ISIN-keyed,
so Vanguard tickers carry no metadata and land in an ``unknown`` asset
class / domicile bucket (flagged). Issuer-level breakdown isn't available
yet — there's no issuer field on the metadata — so domicile stands in as
the geographic-exposure dimension.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from banking_pipeline.balances_extract import extract_balances_from_statement
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.prices_extract import extract_prices_from_statement
from banking_pipeline.tax.uk.currency import RateGap, to_gbp

_ZERO = Decimal(0)
_CASH = "cash"
_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Holding:
    """One valued position at the report date."""

    key: str  # ISIN / ticker / currency (for cash)
    name: str
    asset_class: str
    domicile: str
    currency: str  # quotation currency (cash: the cash currency)
    quantity: Decimal
    value_gbp: Decimal
    is_cash: bool


@dataclass(frozen=True)
class ConcentrationReport:
    as_of: date | None
    # Sum of (positive) security values — the concentration denominator.
    # Cash, including a negative margin/Lombard balance, is financing, not
    # a position you're concentrated in, so it's excluded from the weights
    # and reported separately.
    gross_long_gbp: Decimal
    net_cash_gbp: Decimal  # signed; negative = a margin/Lombard loan
    net_worth_gbp: Decimal  # gross long + net cash
    securities: tuple[Holding, ...]  # valued, sorted by value desc
    cash: tuple[Holding, ...]  # one per currency (netted), sorted by |value|
    # Securities held but with no statement mark, so unvaluable.
    missing_prices: tuple[str, ...]
    # Holdings valued in a non-GBP currency with no rate (excluded).
    rate_gaps: tuple[RateGap, ...]
    # Valued holdings with no commodities.toml metadata (unknown buckets).
    unclassified: tuple[str, ...]


@dataclass(frozen=True)
class _RawHolding:
    portfolio: str
    on_date: date
    key: str
    quantity: Decimal
    price: Decimal | None  # native per-unit mark; None for cash / unpriced
    currency: str
    is_cash: bool


def _is_currency(key: str) -> bool:
    return len(key) == 3 and key.isalpha()


def _portfolio_of(account: str) -> str:
    """``Assets:Pic:K123456001:IE00…`` → ``Assets:Pic:K123456001`` — the
    account minus its commodity/currency leaf, used to keep only the
    latest statement per portfolio."""

    return account.rsplit(":", 1)[0]


def _raw_from_statement(text: str, source: str) -> list[_RawHolding]:
    """Per-holding raw rows from one statement's valuation snapshot.

    Joins the balance quantities to the statement's per-commodity marks;
    a cash sub-account (3-letter currency leaf) becomes a cash holding,
    and a security with a quantity but no mark is kept price-less so it
    can be reported as unvaluable rather than dropped.
    """

    balances = extract_balances_from_statement(text)
    if not balances:
        return []
    prices = extract_prices_from_statement(text, doctype=None, source=source)
    price_map = {p.commodity: (Decimal(p.price), p.currency) for p in prices}

    out: list[_RawHolding] = []
    for date_str, account, amount_str, key in balances:
        on_date = date.fromisoformat(date_str)
        portfolio = _portfolio_of(account)
        amount = Decimal(amount_str)
        if key in price_map:
            price, ccy = price_map[key]
            out.append(
                _RawHolding(portfolio, on_date, key, amount, price, ccy, False)
            )
        elif _is_currency(key):
            out.append(
                _RawHolding(portfolio, on_date, key, amount, None, key, True)
            )
        else:
            out.append(
                _RawHolding(portfolio, on_date, key, amount, None, "", False)
            )
    return out


def build_report(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> ConcentrationReport:
    """Build the concentration report from ``(text, source-name)`` pairs.

    Only the latest statement per portfolio contributes (older snapshots
    are superseded), so passing a whole directory of statements yields the
    current position. Holdings are valued in GBP and sorted by value.
    """

    raws: list[_RawHolding] = []
    for text, source in statements:
        raws.extend(_raw_from_statement(text, source))
    return _build_from_raw(raws, commodities=commodities, rate_source=rate_source)


def _build_from_raw(
    raws: list[_RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> ConcentrationReport:
    """Value + aggregate raw holdings into a report (the testable core)."""

    # Keep only the latest statement date seen for each portfolio.
    latest: dict[str, date] = {}
    for r in raws:
        if r.portfolio not in latest or r.on_date > latest[r.portfolio]:
            latest[r.portfolio] = r.on_date
    current = {
        (r.portfolio, r.key): r
        for r in raws
        if r.on_date == latest[r.portfolio]
    }

    securities: list[Holding] = []
    # Cash is netted across portfolios by currency (a Lombard loan on one
    # account and credit cash on another are one economic FX position).
    cash_gbp: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    cash_native: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    missing_prices: list[str] = []
    rate_gaps: list[RateGap] = []
    unclassified: list[str] = []

    for r in current.values():
        if r.is_cash:
            value_gbp = to_gbp(
                r.quantity, currency=r.currency, on_date=r.on_date,
                source=rate_source,
            )
            if value_gbp is None:
                rate_gaps.append(
                    RateGap(isin=r.key, currency=r.currency,
                            month=r.on_date.strftime("%Y-%m"))
                )
                continue
            cash_gbp[r.currency] += value_gbp
            cash_native[r.currency] += r.quantity
            continue

        if r.price is None:
            missing_prices.append(r.key)
            continue
        meta = commodities.get(r.key)
        value_gbp = to_gbp(
            r.quantity * r.price, currency=r.currency, on_date=r.on_date,
            source=rate_source,
        )
        if value_gbp is None:
            rate_gaps.append(
                RateGap(isin=r.key, currency=r.currency,
                        month=r.on_date.strftime("%Y-%m"))
            )
            continue
        if meta is None:
            unclassified.append(r.key)
        securities.append(
            Holding(
                key=r.key, name=meta.name if meta else r.key,
                asset_class=meta.asset_class if meta else _UNKNOWN,
                domicile=meta.domicile if meta else _UNKNOWN,
                currency=r.currency, quantity=r.quantity, value_gbp=value_gbp,
                is_cash=False,
            )
        )

    securities.sort(key=lambda h: h.value_gbp, reverse=True)
    cash = tuple(
        Holding(
            key=ccy, name=f"Cash ({ccy})", asset_class=_CASH, domicile="—",
            currency=ccy, quantity=cash_native[ccy], value_gbp=cash_gbp[ccy],
            is_cash=True,
        )
        for ccy in sorted(cash_gbp, key=lambda c: abs(cash_gbp[c]), reverse=True)
    )

    gross_long = sum((h.value_gbp for h in securities if h.value_gbp > _ZERO), _ZERO)
    net_cash = sum(cash_gbp.values(), _ZERO)
    net_worth = sum((h.value_gbp for h in securities), _ZERO) + net_cash
    as_of = max(latest.values()) if latest else None
    return ConcentrationReport(
        as_of=as_of,
        gross_long_gbp=gross_long,
        net_cash_gbp=net_cash,
        net_worth_gbp=net_worth,
        securities=tuple(securities),
        cash=cash,
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
        lines.append(f"| {label} | {_gbp(value)} | {_pct(value, total)} |")
    lines.append("")
    return lines


def render_markdown(report: ConcentrationReport) -> str:
    as_of = report.as_of.isoformat() if report.as_of else "—"
    gross = report.gross_long_gbp
    lines = [
        "# Portfolio concentration",
        "",
        f"As at **{as_of}**. Gross long holdings: **{_gbp(gross)}**; "
        f"net cash: **{_gbp(report.net_cash_gbp)}**; net worth: "
        f"**{_gbp(report.net_worth_gbp)}**. Weights below are a share of "
        "gross long holdings (cash / leverage shown separately). Values are "
        "statement marks converted to GBP — a reporting aid, not advice.",
        "",
    ]
    if report.net_cash_gbp < _ZERO:
        lines += [
            f"> The portfolio is **leveraged**: net cash is "
            f"{_gbp(report.net_cash_gbp)} (a margin / Lombard loan). Gross "
            f"long holdings of {_gbp(gross)} are funded partly by borrowing, "
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
                f"| {c.currency} | {_gbp(c.value_gbp)} | {_pct(c.value_gbp, gross)} |"
            )
        lines.append(f"| **Net cash** | {_gbp(report.net_cash_gbp)} | "
                     f"{_pct(report.net_cash_gbp, gross)} |")
        lines.append("")

    if report.missing_prices:
        lines += [
            "## ⚠️ Unvaluable holdings (no statement mark)",
            "",
            "Held but excluded from the figures above — the latest statement "
            "carried no price for them:",
            "",
        ]
        lines += [f"- {k}" for k in report.missing_prices]
        lines.append("")
    if report.rate_gaps:
        uniq = sorted(set(report.rate_gaps), key=lambda g: (g.currency, g.isin))
        lines += [
            "## ⚠️ Excluded — missing GBP rate",
            "",
            "Valued in a non-GBP currency with no rate, so excluded (the "
            "weights above understate). Add the month/currency to "
            "`data/fx/hmrc-monthly-average.csv` and re-run:",
            "",
        ]
        lines += [f"- {g.currency} {g.month} ({g.isin})" for g in uniq]
        lines.append("")
    if report.unclassified:
        lines += [
            "## ⚠️ Unclassified holdings (no metadata)",
            "",
            "Counted by value but bucketed `unknown` for asset class and "
            "domicile — add them to `data/commodities.toml` for accurate "
            "breakdowns:",
            "",
        ]
        lines += [f"- {k}" for k in report.unclassified]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: ConcentrationReport) -> list[list[str]]:
    """Per-holding rows for the CSV (header first). Securities then cash;
    ``weight_pct`` is a share of gross long holdings (blank for cash)."""

    gross = report.gross_long_gbp

    def _weight(value: Decimal) -> str:
        if gross == _ZERO:
            return ""
        return str((value / gross * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

    rows = [[
        "kind", "key", "name", "asset_class", "domicile", "currency",
        "quantity", "value_gbp", "weight_pct",
    ]]
    for h in report.securities:
        rows.append([
            "security", h.key, h.name, h.asset_class, h.domicile, h.currency,
            _money(h.quantity), _money(h.value_gbp), _weight(h.value_gbp),
        ])
    for c in report.cash:
        rows.append([
            "cash", c.key, c.name, c.asset_class, c.domicile, c.currency,
            _money(c.quantity), _money(c.value_gbp), "",
        ])
    return rows
