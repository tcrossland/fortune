"""Vanguard ISA regular-statement valuation parser.

The ``Your ISA investments at <date>`` table on a Vanguard regular
statement is a point-in-time snapshot — one row per holding plus a cash
row::

    Your ISA investments at 12 May 2025
    Description Quantity Price Value
    FTSE 250 UCITS ETF - Accumulating (VMIG) 13.00 £37.41 £486.26
    U.K. Gilt UCITS ETF - Accumulating (VGVA) 25.00 £19.92 £497.90
    Cash account - - £17.00

This module parses that table into a :class:`IsaValuation` — the shared
substrate that :mod:`banking_pipeline.balances_extract` turns into
``balance`` assertions and :mod:`banking_pipeline.prices_extract` turns
into ``price`` directives. It deliberately does **not** read the
activity table (that's the transaction template's job); it only reads
the valuation snapshot.

Two real-world quirks it absorbs:

  - **Movement-pair rows.** When a fund is fully traded out within the
    period, Vanguard prints both legs (``-13.00`` and ``13.00``) so the
    snapshot nets to zero. Quantities are summed per ticker, so a
    wound-down position correctly reads as 0 units.
  - **Missing tickers.** Some rows omit the ``(VMIG)`` parenthetical and
    print only the fund name, so the ticker is resolved via
    :func:`banking_pipeline.templates.vanguard_uk._common.resolve_ticker`
    (the same name→ticker map the trade templates use).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.templates.vanguard_uk._common import (
    find_account_number,
    parse_long_date,
    resolve_ticker,
)

# Anchor + section bounds. The valuation table opens at "Your ISA
# investments at <date>" and runs until the activity table or the
# protection notice.
_SECTION_RE = re.compile(
    r"Your\s+ISA\s+investments\s+at\s+(?P<date>\d{1,2}\s+\w+\s+\d{4})"
    r"(?P<body>.*?)"
    r"(?:Activity\s+from|Your\s+cash\s+and\s+asset\s+protection|\Z)",
    re.S | re.I,
)
# A holding row: ``<name>[ (TICKER)] <qty> £<price> £<value>``. Quantity
# and value may be negative (movement-pair legs).
_HOLDING_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<qty>-?[\d,]+\.\d{2})\s+£(?P<price>[\d,]+\.\d+)"
    r"\s+-?£[\d,]+\.\d{2}\s*$",
    re.M,
)
# The cash row: ``Cash account - - £<balance>``.
_CASH_RE = re.compile(r"^Cash account\s+-\s+-\s+£(?P<bal>[\d,]+\.\d{2})\s*$", re.M)
# Trailing ``(TICKER)`` on a fund name.
_NAME_TICKER_RE = re.compile(r"\s*\((?P<ticker>[A-Z0-9]{2,6})\)\s*$")
# The two-column summary a regular statement always prints:
#   ``Product Value on <prior date> Value on <current date>``
#   ``Account total £<prior> £<current>``
# The *current* (rightmost) column is this statement's balance.
_PERIOD_RE = re.compile(
    r"Value on\s+\d{1,2}\s+\w+\s+\d{4}\s+Value on\s+(?P<current>\d{1,2}\s+\w+\s+\d{4})",
    re.I,
)
_ACCOUNT_TOTAL_RE = re.compile(
    r"Account total\s+£[\d,]+\.\d{2}\s+£(?P<current>[\d,]+\.\d{2})", re.I
)


@dataclass(frozen=True)
class IsaHolding:
    ticker: str
    quantity: Decimal  # net units held at the statement date
    price: Decimal  # GBP per unit, the statement's mark


@dataclass(frozen=True)
class IsaValuation:
    statement_date: date
    account_number: str | None
    holdings: tuple[IsaHolding, ...]
    cash_balance: Decimal | None  # GBP, or None when no cash row is printed


@dataclass(frozen=True)
class IsaClosure:
    """A *wound-down* ISA: the regular statement's current-column account
    total is nil. Carries the statement date and account so a timeline can
    retire the portfolio at the drain date rather than carry its last
    non-empty snapshot forward indefinitely."""

    statement_date: date
    account_number: str | None


def _money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def parse_isa_nil_statement(text: str) -> IsaClosure | None:
    """Recognise a *drained* Vanguard ISA regular statement — one whose
    current-column ``Account total`` is £0.00 — and return its closure
    marker, else ``None``.

    Keyed on the statement's own explicit nil total, **not** the absence of
    a parsed valuation table: a still-funded account always prints a non-zero
    current total, so a parser miss on a live statement can never be mistaken
    for a wind-down (which would phantom-collapse net worth). The two markers
    (period line + account-total line) must both be present, so a non-Vanguard
    or non-regular-statement document returns ``None``."""

    period = _PERIOD_RE.search(text)
    total = _ACCOUNT_TOTAL_RE.search(text)
    if period is None or total is None:
        return None
    if _money(total.group("current")) != 0:
        return None
    return IsaClosure(
        statement_date=parse_long_date(period.group("current")),
        account_number=find_account_number(text),
    )


def parse_isa_valuation(text: str) -> IsaValuation | None:
    """Parse the ``Your ISA investments at …`` snapshot, or ``None``.

    Returns ``None`` when the statement carries no valuation section
    (e.g. a drained / £0 account whose statement omits the table). When
    the section is present but empty (all positions sold, no cash row),
    returns an :class:`IsaValuation` with no holdings and ``cash_balance``
    ``None`` — callers then emit nothing, which is correct.

    Quantities are summed per ticker (so movement-pair rows net out);
    the price kept per ticker is the last row's (all legs of a pair
    carry the same mark).
    """

    section = _SECTION_RE.search(text)
    if section is None:
        return None

    statement_date = parse_long_date(section.group("date"))
    body = section.group("body")

    qty_by_ticker: dict[str, Decimal] = {}
    price_by_ticker: dict[str, Decimal] = {}
    for m in _HOLDING_RE.finditer(body):
        raw_name = m.group("name").strip()
        tm = _NAME_TICKER_RE.search(raw_name)
        parens = tm.group("ticker") if tm else None
        name = _NAME_TICKER_RE.sub("", raw_name).strip()
        ticker = resolve_ticker(name, parens)
        if ticker is None:
            continue
        qty_by_ticker[ticker] = qty_by_ticker.get(ticker, Decimal(0)) + _money(
            m.group("qty")
        )
        price_by_ticker[ticker] = _money(m.group("price"))

    holdings = tuple(
        IsaHolding(ticker=t, quantity=qty_by_ticker[t], price=price_by_ticker[t])
        for t in sorted(qty_by_ticker)
    )

    cash_match = _CASH_RE.search(body)
    cash_balance = _money(cash_match.group("bal")) if cash_match else None

    return IsaValuation(
        statement_date=statement_date,
        account_number=find_account_number(text),
        holdings=holdings,
        cash_balance=cash_balance,
    )
