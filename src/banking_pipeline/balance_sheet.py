"""Interactive balance-sheet dataset (phase 1: data extraction).

Builds the serialisable dataset behind the ``balance-sheet`` command: a
single JSON blob the browser aggregates to *any* as-of date with no server
round-trip. The artifact (template + inliner + chart) is phase 2; this
module owns only the numbers.

Why ``bean-query`` and not a hand-rolled parser
-----------------------------------------------
A balance sheet needs *booked, balanced* postings. beancount's loader runs
FIFO booking and balances the elastic Income/Expense legs before a query
sees a row, so we get authoritative units for free — and stay the right
side of the GPL boundary by shelling out (never ``import beancount``), as
:mod:`banking_pipeline.bean_query` already does. The decisive win: a
market-value sheet needs only **unit sums** per ``(account, commodity)`` up
to the as-of date, and FIFO never changes the *total* units held — only
which lot a sale draws from — so summing posting units client-side is
exact. (Cost basis *is* date-dependent; that's a non-goal here.)

Scope (phase 1)
---------------
Only ``Assets`` / ``Liabilities`` postings are queried: those are the
holdings a market-value sheet values, and in this book the Lombard loan is
a **negative cash balance** on an ``Assets:…:<CCY>`` sub-account, not a
``Liabilities:`` entry (see design-decisions). Equity / Income / Expense
flows are deferred (the "where did the money come from" view is a non-goal).

Currency → GBP
--------------
``data/prices.beancount`` carries only *security* marks (in the security's
quote currency — EUR/USD/GBP). It has **no** currency→GBP directives, so
every non-GBP currency's GBP series is synthesised from the injected
:class:`GbpRateSource` (the same HMRC monthly-average source the
``concentration`` / ``net-worth`` reports use) across the data's month
span. A currency with no rate at all surfaces with an empty series — the
browser flags such a holding rather than valuing it at zero.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from banking_pipeline.bean_query import QueryResult, run_query
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.trial_balance import parse_amounts

OPERATING_CURRENCY = "GBP"

# One row per Asset/Liability posting: the transaction date, the account,
# and the posting's units (``units(position)`` strips cost so a security
# bought ``{5.00 USD}`` reads simply ``10 HOOL``). Ungrouped, so bean-query
# returns one row per leg rather than an aggregate.
_BQL = (
    "SELECT date, account, units(position) "
    "WHERE account ~ '^(Assets|Liabilities):' "
    "ORDER BY date"
)

# ``<date> price <commodity> <price> <ccy>`` (trailing ``; source:`` comment
# ignored). The commodity is an ISIN / internal-ref / ticker; the price is a
# plain decimal; the currency is the security's quote currency.
_PRICE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+price\s+(\S+)\s+([\d.]+)\s+([A-Z]{3})\b"
)
# ``<date> balance <account> <qty> [~ <tol>] <ccy>`` — the optional ``~ tol``
# is dropped (we keep only the asserted quantity).
_BALANCE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+balance\s+(\S+)\s+(-?[\d.]+)"
    r"(?:\s+~\s+[\d.]+)?\s+([A-Z0-9]+)\b"
)


@dataclass(frozen=True)
class Posting:
    """One Asset/Liability posting leg, units only (cost stripped)."""

    date: date
    account: str
    quantity: Decimal
    commodity: str


@dataclass(frozen=True)
class PricePoint:
    """A ``price`` of one commodity in ``currency`` on ``date``."""

    date: date
    price: Decimal
    currency: str


@dataclass(frozen=True)
class CommodityInfo:
    """Display metadata for a held security commodity."""

    description: str
    asset_class: str
    domicile: str


@dataclass(frozen=True)
class Assertion:
    """A ledger ``balance`` directive — the statement-vs-ledger drift overlay
    substrate (rendered in phase 4; carried in the dataset now)."""

    date: date
    account: str
    quantity: Decimal
    commodity: str


@dataclass(frozen=True)
class BalanceSheetData:
    operating_currency: str
    as_of_min: date
    as_of_max: date
    postings: tuple[Posting, ...]
    # commodity / currency -> ascending-by-date price series. A security key
    # maps to marks in its quote currency; a currency key (e.g. ``"EUR"``)
    # maps to that currency's GBP rate series.
    prices: dict[str, tuple[PricePoint, ...]]
    commodities: dict[str, CommodityInfo]
    assertions: tuple[Assertion, ...]


def _is_currency(token: str) -> bool:
    return len(token) == 3 and token.isalpha()


def parse_postings(result: QueryResult) -> tuple[Posting, ...]:
    """Flatten a ``date, account, units`` bean-query result into postings.

    Rows whose unit cell is empty or unparseable (a zero leg) are dropped —
    ``units(position)`` yields one ``(amount, commodity)`` pair per posting,
    parsed with the same helper the trial balance uses.
    """

    postings: list[Posting] = []
    for row in result.rows:
        if len(row) < 3:
            continue
        date_str, account, units_field = row[0].strip(), row[1].strip(), row[2]
        try:
            on_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        for amount, commodity in parse_amounts(units_field):
            postings.append(Posting(on_date, account, amount, commodity))
    return tuple(postings)


def parse_price_directives(text: str) -> dict[str, tuple[PricePoint, ...]]:
    """Parse ``price`` directives from a ``prices.beancount`` body.

    Returns ``{commodity: ascending-by-date series}``. Comments and
    non-price lines are skipped; the trailing ``; source:`` annotation is
    ignored.
    """

    series: dict[str, list[PricePoint]] = {}
    for line in text.splitlines():
        m = _PRICE_RE.match(line.strip())
        if m is None:
            continue
        on_date = date.fromisoformat(m.group(1))
        commodity, price_str, ccy = m.group(2), m.group(3), m.group(4)
        try:
            price = Decimal(price_str)
        except InvalidOperation:
            continue
        series.setdefault(commodity, []).append(
            PricePoint(on_date, price, ccy)
        )
    return {
        c: tuple(sorted(pts, key=lambda p: p.date)) for c, pts in series.items()
    }


def parse_balance_assertions(text: str) -> tuple[Assertion, ...]:
    """Parse ``balance`` directives from a ``balances.beancount`` body."""

    out: list[Assertion] = []
    for line in text.splitlines():
        m = _BALANCE_RE.match(line.strip())
        if m is None:
            continue
        try:
            qty = Decimal(m.group(3))
        except InvalidOperation:
            continue
        out.append(
            Assertion(date.fromisoformat(m.group(1)), m.group(2), qty, m.group(4))
        )
    return tuple(out)


def _month_starts(start: date, end: date) -> list[date]:
    """First-of-month dates from ``start``'s month through ``end``'s month."""

    months: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _synthesize_fx(
    currencies: set[str],
    *,
    as_of_min: date,
    as_of_max: date,
    rate_source: GbpRateSource,
) -> dict[str, tuple[PricePoint, ...]]:
    """Build a GBP rate series for each non-GBP currency from ``rate_source``.

    One point per month across ``[as_of_min, as_of_max]`` (the HMRC source
    is monthly-average, and monthly granularity lets the browser pick a
    near-contemporaneous rate for any as-of). A month with no rate is
    skipped; a currency with no rate at all yields an empty series and is
    flagged — never silently valued — downstream.
    """

    months = _month_starts(as_of_min, as_of_max)
    out: dict[str, tuple[PricePoint, ...]] = {}
    for ccy in currencies:
        if ccy == OPERATING_CURRENCY:
            continue
        points = [
            PricePoint(m, rate, OPERATING_CURRENCY)
            for m in months
            if (rate := rate_source.get_rate(m, ccy)) is not None
        ]
        if points:
            out[ccy] = tuple(points)
    return out


def assemble(
    postings: tuple[Posting, ...],
    marks: dict[str, tuple[PricePoint, ...]],
    commodities: dict[str, CommodityMetadata],
    assertions: tuple[Assertion, ...],
    *,
    rate_source: GbpRateSource,
    operating_currency: str = OPERATING_CURRENCY,
) -> BalanceSheetData:
    """Assemble the dataset from already-parsed inputs (the pure core).

    Computes the as-of bounds, synthesises the currency→GBP series the
    security marks don't carry, and builds the per-security display map.
    Pure and binary-free, so the whole transform is unit-testable.
    """

    dates = (
        [p.date for p in postings]
        + [pt.date for s in marks.values() for pt in s]
        + [a.date for a in assertions]
    )
    if dates:
        as_of_min, as_of_max = min(dates), max(dates)
    else:  # empty ledger — degenerate but valid
        as_of_min = as_of_max = date(1970, 1, 1)

    # Currencies needing a GBP series: cash held directly, plus the quote
    # currency of every security mark (so a EUR-quoted fund can chain to GBP).
    currencies = {p.commodity for p in postings if _is_currency(p.commodity)}
    currencies |= {pt.currency for s in marks.values() for pt in s}
    fx = _synthesize_fx(
        currencies, as_of_min=as_of_min, as_of_max=as_of_max,
        rate_source=rate_source,
    )

    # Marks win over synthesised FX on a key collision (a security never
    # shares a key with a currency, so this only guards against odd data).
    prices: dict[str, tuple[PricePoint, ...]] = {**fx, **marks}

    # Display metadata for every held *security* commodity (currencies need
    # none). Fall back to the bare code when the commodity isn't in
    # commodities.toml so the holding still renders, grouped under "other".
    info: dict[str, CommodityInfo] = {}
    for commodity in {p.commodity for p in postings if not _is_currency(p.commodity)}:
        meta = commodities.get(commodity)
        info[commodity] = (
            CommodityInfo(meta.name, meta.asset_class, meta.domicile)
            if meta is not None
            else CommodityInfo(commodity, "other", "")
        )

    return BalanceSheetData(
        operating_currency=operating_currency,
        as_of_min=as_of_min,
        as_of_max=as_of_max,
        postings=postings,
        prices=prices,
        commodities=info,
        assertions=assertions,
    )


def build_data(
    ledger: Path,
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
    prices_path: Path | None = None,
    assertions_path: Path | None = None,
) -> tuple[BalanceSheetData | None, QueryResult]:
    """Build the dataset for ``ledger``.

    The single shell-out (``run_query``) is isolated here; everything after
    is the pure :func:`assemble`. Returns ``(data, query_result)`` — ``data``
    is ``None`` when ``bean-query`` is missing or errored, so the caller can
    degrade with a warning (mirroring the trial balance / scorecard) rather
    than crash. ``prices_path`` / ``assertions_path`` are read when present;
    a missing file is treated as empty.
    """

    result = run_query(ledger, _BQL)
    if not result.ok:
        return None, result

    postings = parse_postings(result)
    marks = (
        parse_price_directives(prices_path.read_text(encoding="utf-8"))
        if prices_path is not None and prices_path.exists()
        else {}
    )
    assertions = (
        parse_balance_assertions(assertions_path.read_text(encoding="utf-8"))
        if assertions_path is not None and assertions_path.exists()
        else ()
    )
    data = assemble(
        postings, marks, commodities, assertions, rate_source=rate_source
    )
    return data, result


@dataclass(frozen=True)
class AccountValue:
    """One account's signed GBP market value at an as-of date."""

    account: str
    value_gbp: Decimal


@dataclass(frozen=True)
class Valuation:
    """The whole book valued at one as-of date — the reference the
    template's JavaScript mirrors (see :func:`value_as_of`)."""

    as_of: date
    accounts: tuple[AccountValue, ...]
    assets_gbp: Decimal
    liabilities_gbp: Decimal  # positive magnitude of the negative balances
    net_worth_gbp: Decimal
    by_asset_class: dict[str, Decimal]  # gross-long GBP per class
    missing: tuple[str, ...]  # "account:commodity" with no price <= as_of


def _latest(series: tuple[PricePoint, ...], as_of: date) -> PricePoint | None:
    """The last price point on or before ``as_of`` (series is ascending)."""

    best: PricePoint | None = None
    for pt in series:
        if pt.date <= as_of:
            best = pt
        else:
            break
    return best


def _value_leg(
    data: BalanceSheetData, commodity: str, qty: Decimal, as_of: date
) -> Decimal | None:
    """GBP value of ``qty`` units of ``commodity`` at ``as_of``, or ``None``
    when no price (mark or FX rate) is available on or before that date.

    Chains commodity → quote-currency → GBP: GBP cash is 1:1, other cash
    uses its synthesised GBP series, and a security uses its mark then (if
    quoted in a foreign currency) that currency's GBP series.
    """

    if commodity == OPERATING_CURRENCY:
        return qty
    if _is_currency(commodity):
        rate = _latest(data.prices.get(commodity, ()), as_of)
        return qty * rate.price if rate is not None else None
    mark = _latest(data.prices.get(commodity, ()), as_of)
    if mark is None:
        return None
    value = qty * mark.price
    if mark.currency == OPERATING_CURRENCY:
        return value
    rate = _latest(data.prices.get(mark.currency, ()), as_of)
    return value * rate.price if rate is not None else None


def value_as_of(data: BalanceSheetData, as_of: date) -> Valuation:
    """Value the whole book at ``as_of`` — the Python reference for the
    client-side aggregation.

    Sums posting units per ``(account, commodity)`` up to ``as_of`` (FIFO
    never changes total units, so this is exact), values each holding to
    GBP, and folds into per-account totals, the Assets / Liabilities /
    net-worth figures (positive account balances are assets; negative ones
    — the Lombard loan — are liabilities), and a gross-long allocation by
    asset class. A holding with no price is surfaced in ``missing``, never
    valued at zero. The template's JavaScript is a thin port of this.
    """

    balances: dict[tuple[str, str], Decimal] = {}
    for p in data.postings:
        if p.date <= as_of:
            key = (p.account, p.commodity)
            balances[key] = balances.get(key, Decimal(0)) + p.quantity

    account_totals: dict[str, Decimal] = {}
    by_class: dict[str, Decimal] = {}
    missing: list[str] = []
    for (account, commodity), qty in balances.items():
        if qty == 0:
            continue
        gbp = _value_leg(data, commodity, qty, as_of)
        if gbp is None:
            missing.append(f"{account}:{commodity}")
            continue
        account_totals[account] = account_totals.get(account, Decimal(0)) + gbp
        if gbp > 0:
            cls = "cash" if _is_currency(commodity) else _asset_class_of(
                data, commodity
            )
            by_class[cls] = by_class.get(cls, Decimal(0)) + gbp

    accounts = tuple(
        AccountValue(a, v) for a, v in sorted(account_totals.items()) if v != 0
    )
    assets = sum((v for v in account_totals.values() if v > 0), Decimal(0))
    liabilities = -sum((v for v in account_totals.values() if v < 0), Decimal(0))
    return Valuation(
        as_of=as_of,
        accounts=accounts,
        assets_gbp=assets,
        liabilities_gbp=liabilities,
        net_worth_gbp=assets - liabilities,
        by_asset_class=by_class,
        missing=tuple(sorted(missing)),
    )


def _asset_class_of(data: BalanceSheetData, commodity: str) -> str:
    info = data.commodities.get(commodity)
    return info.asset_class if info is not None else "other"


def to_json(data: BalanceSheetData) -> str:
    """Serialise to the compact, browser-facing JSON.

    Short keys on the big arrays (``d``/``a``/``q``/``c`` for postings,
    ``d``/``p``/``c`` for price points) keep the inlined artifact small;
    decimals are emitted as strings so the browser doesn't lose precision.
    """

    payload = {
        "operating_currency": data.operating_currency,
        "as_of_min": data.as_of_min.isoformat(),
        "as_of_max": data.as_of_max.isoformat(),
        "postings": [
            {
                "d": p.date.isoformat(),
                "a": p.account,
                "q": str(p.quantity),
                "c": p.commodity,
            }
            for p in data.postings
        ],
        "prices": {
            commodity: [
                {"d": pt.date.isoformat(), "p": str(pt.price), "c": pt.currency}
                for pt in series
            ]
            for commodity, series in data.prices.items()
        },
        "commodities": {
            commodity: {
                "description": ci.description,
                "asset_class": ci.asset_class,
                "domicile": ci.domicile,
            }
            for commodity, ci in data.commodities.items()
        },
        "assertions": [
            {
                "d": a.date.isoformat(),
                "a": a.account,
                "q": str(a.quantity),
                "c": a.commodity,
            }
            for a in data.assertions
        ],
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


_TEMPLATE_PATH = Path(__file__).with_name("balance_sheet_template.html")
_DATA_TOKEN = '"__DATA_PLACEHOLDER__"'


def render_html(data: BalanceSheetData) -> str:
    """Inline ``data`` into the committed template → a standalone HTML file.

    The template carries one ``"__DATA_PLACEHOLDER__"`` token (a quoted JS
    string); substituting the JSON object for it yields a single
    self-contained, offline artifact — no server, no network. ``</`` in the
    JSON is escaped so a stray ``</script>`` inside a fund description can't
    break out of the script element (``<\\/script>`` still parses back to
    the original inside JSON).
    """

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    inlined = to_json(data).replace("</", "<\\/")
    return template.replace(_DATA_TOKEN, inlined)
