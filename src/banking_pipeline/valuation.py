"""Statement-valuation core.

Values a set of holdings (securities at ``qty x mark``, cash netted by
currency) to GBP and aggregates them into a :class:`ValuationResult`. The
shared engine behind every valuation report: ``concentration`` (latest
snapshot per portfolio), ``net-worth`` and ``allocation`` (a timeline of
snapshots), and ``portfolio-allocation`` (per-portfolio). It owns the
raw-holding model and the statement parser; the report modules add their
own grouping + Markdown/CSV rendering on top.

Values are statement marks converted to GBP at the configured rate. A
holding with no mark, or one that cannot be converted to GBP, is excluded
and surfaced as a warning rather than silently understating a weight.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.balances_extract import extract_balances_from_statement
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.prices_extract import extract_prices_from_statement
from banking_pipeline.property import Property
from banking_pipeline.tax.uk.currency import RateGap, to_gbp

_ZERO = Decimal(0)
_CASH = "cash"
_UNKNOWN = "unknown"
_PROPERTY = "property"


def as_of[T](items: list[T], on_date: date, *, key: Callable[[T], date]) -> T | None:
    """The last item whose ``key(item)`` falls on or before ``on_date``.

    Assumes ``items`` is sorted ascending by ``key`` — the as-of forward-fill
    the timeline reports use to carry each portfolio's latest snapshot
    forward to a given date.
    """

    chosen: T | None = None
    for item in items:
        if key(item) <= on_date:
            chosen = item
        else:
            break
    return chosen


def property_raws(properties: list[Property]) -> list[RawHolding]:
    """Turn properties into raw holdings (one per valuation mark) so they
    flow through the same valuation as statement holdings: 1 unit valued at
    the mark, tagged ``asset_class="property"`` / domicile = country. Each
    property is its own pseudo-portfolio, so latest-per-portfolio (the
    concentration view) keeps the most recent mark and the net-worth
    timeline carries every mark."""

    out: list[RawHolding] = []
    for p in properties:
        for v in p.marks():
            out.append(
                RawHolding(
                    portfolio=f"Property:{p.label}", on_date=v.date,
                    key=p.commodity, quantity=Decimal(1), price=v.value,
                    currency=p.currency, is_cash=False, label=p.display_name,
                    asset_class=_PROPERTY, domicile=p.country,
                    issuer=_PROPERTY.capitalize(),
                )
            )
    return out


@dataclass(frozen=True)
class Holding:
    """One valued position at the report date."""

    key: str  # ISIN / ticker / currency (for cash)
    name: str
    asset_class: str
    domicile: str
    issuer: str  # fund house / manager; "unknown" when un-inferable
    currency: str  # quotation currency (cash: the cash currency)
    quantity: Decimal
    value_gbp: Decimal
    is_cash: bool


@dataclass(frozen=True)
class ValuationResult:
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
class RawHolding:
    portfolio: str
    on_date: date
    key: str
    quantity: Decimal
    price: Decimal | None  # native per-unit mark; None for cash / unpriced
    currency: str
    is_cash: bool
    # Overrides for non-statement holdings (e.g. property), which carry no
    # commodities.toml entry. When set, they bypass the metadata lookup.
    label: str | None = None
    asset_class: str | None = None
    domicile: str | None = None
    issuer: str | None = None


def _is_currency(key: str) -> bool:
    return len(key) == 3 and key.isalpha()


def _portfolio_of(account: str) -> str:
    """``Assets:Pic:K123456001:IE00…`` → ``Assets:Pic:K123456001`` — the
    account minus its commodity/currency leaf, used to keep only the
    latest statement per portfolio."""

    return account.rsplit(":", 1)[0]


def raw_from_statement(text: str, source: str) -> list[RawHolding]:
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

    out: list[RawHolding] = []
    for date_str, account, amount_str, key in balances:
        on_date = date.fromisoformat(date_str)
        portfolio = _portfolio_of(account)
        amount = Decimal(amount_str)
        if key in price_map:
            price, ccy = price_map[key]
            out.append(
                RawHolding(portfolio, on_date, key, amount, price, ccy, False)
            )
        elif _is_currency(key):
            out.append(
                RawHolding(portfolio, on_date, key, amount, None, key, True)
            )
        else:
            out.append(
                RawHolding(portfolio, on_date, key, amount, None, "", False)
            )
    return out



def value_holdings(
    raws: list[RawHolding],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> ValuationResult:
    """Value a set of raw holdings to GBP and aggregate (no latest-per-
    portfolio filtering — the caller decides what to pass). Securities are
    valued at ``qty × mark``; cash is netted by currency; everything is
    converted at each holding's statement date. Shared by the concentration
    report and the net-worth timeline."""

    securities: list[Holding] = []
    # Cash is netted across portfolios by currency (a Lombard loan on one
    # account and credit cash on another are one economic FX position).
    cash_gbp: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    cash_native: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    missing_prices: list[str] = []
    rate_gaps: list[RateGap] = []
    unclassified: list[str] = []

    for r in raws:
        if r.is_cash:
            value_gbp = to_gbp(
                r.quantity, currency=r.currency, on_date=r.on_date,
                source=rate_source,
            )
            if value_gbp is None:
                rate_gaps.append(RateGap.at(r.key, r.currency, r.on_date))
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
            rate_gaps.append(RateGap.at(r.key, r.currency, r.on_date))
            continue
        if meta is None and r.asset_class is None:
            unclassified.append(r.key)
        securities.append(
            Holding(
                key=r.key,
                name=r.label or (meta.name if meta else r.key),
                asset_class=r.asset_class or (meta.asset_class if meta else _UNKNOWN),
                domicile=r.domicile or (meta.domicile if meta else _UNKNOWN),
                issuer=r.issuer or (meta.resolved_issuer if meta else None) or _UNKNOWN,
                currency=r.currency, quantity=r.quantity, value_gbp=value_gbp,
                is_cash=False,
            )
        )

    securities.sort(key=lambda h: h.value_gbp, reverse=True)
    cash = tuple(
        Holding(
            key=ccy, name=f"Cash ({ccy})", asset_class=_CASH, domicile="—",
            issuer="—", currency=ccy, quantity=cash_native[ccy],
            value_gbp=cash_gbp[ccy], is_cash=True,
        )
        for ccy in sorted(cash_gbp, key=lambda c: abs(cash_gbp[c]), reverse=True)
    )

    gross_long = sum((h.value_gbp for h in securities if h.value_gbp > _ZERO), _ZERO)
    net_cash = sum(cash_gbp.values(), _ZERO)
    net_worth = sum((h.value_gbp for h in securities), _ZERO) + net_cash
    as_of = max((r.on_date for r in raws), default=None)
    return ValuationResult(
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

