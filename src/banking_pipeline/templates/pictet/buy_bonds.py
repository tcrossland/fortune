"""``pictet.buy_bonds.v1`` — bond purchase advice.

Pictet emits this document under ``STOCK EXCHANGE / Purchase`` when a
bond is bought through the OTC desk. Field shape mirrors
``SELL_BONDS`` with the sign-conventions flipped:

  - ``Executed nominal EUR 90'000.00`` (positive — units coming in;
    sells print this negative).
  - ``Execution price 97.512%`` — a percentage of face value, the
    same structural marker that distinguishes bonds from
    fund/ETF/structured-product trades. Per-unit price for beancount
    = percentage / 100.
  - ``CASH EFFECT`` block carries an ``Interest`` line — accrued
    interest the buyer pays to the seller for the period since the
    last coupon. Pictet prints it negative on the buy side
    (cash-out from the buyer); the writer's bond renderer flips the
    sign on emit so the income account is debited (income lowered),
    consistent with the bond sale renderer crediting the same
    account when the buyer's interest hits the seller's cash.
  - ``Operation type Purchase`` (the bond-buy vocabulary; bond sells
    use ``Sell``, fund subscriptions also use ``Purchase`` but
    differ on every other structural marker).
  - ``Costs`` block carries ``Brokerage`` rather than ``Commission/Fee``
    on the per-line breakdown; the rolled-up ``Costs <ccy> <amount>``
    line in the CASH EFFECT block is the same shape as on sells.

The five-leg render shape:

  - Asset leg (positive nominal entering inventory at cost basis
    ``{<unit_price> <ccy>}`` — buys carry a literal cost-basis brace
    rather than the empty-cost form sells use).
  - Fees leg (Brokerage expense, positive).
  - Accrued-interest leg (``Income:<prefix>:<isin>:Interest`` debited
    by the buyer's accrued payment so coupon yield can be tracked
    cleanly across the holding period).
  - Cash leg (negative — the all-in debit Pictet posted to the
    portfolio's current account).

No ``:Realized`` leg — buys don't realise anything; the asset's cost
basis is what the realised-gain calculation will draw on at sale time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_field,
    find_headline,
    find_transaction_number,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
    resolve_isin,
)

_PRICE_PCT_RE = re.compile(r"^Execution\s+price\s+(-?\d+(?:\.\d+)?)\s*%\s*$", re.M)
_INTEREST_RE = re.compile(
    r"^Interest\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$", re.M
)


@dataclass
class PictetBuyBondsTemplate:
    template_id: str = "pictet.buy_bonds.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Operation type Purchase`` is the buy-side vocabulary; bond
        # sells carry ``Sell``. ``Purchase`` is shared with fund
        # subscriptions, but the other markers below (``Executed
        # nominal`` + percentage-priced ``Execution price``) keep the
        # two apart at the template layer.
        op = find_field(text, "Operation type")
        if op != "Purchase":
            return []

        nominal_match = re.search(
            r"^Executed\s+nominal\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$",
            text,
            re.M,
        )
        if nominal_match is None:
            return []
        nominal_currency = nominal_match.group(1)
        quantity = parse_pictet_amount(nominal_match.group(2))

        price_match = _PRICE_PCT_RE.search(text)
        if price_match is None:
            return []
        price = parse_pictet_amount(price_match.group(1)) / Decimal("100")

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, EN_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)

        costs_match = find_amount_field(text, EN_LABELS.costs)
        fees_currency = costs_match[0] if costs_match else None
        fees = costs_match[1] if costs_match else None

        interest_match = _INTEREST_RE.search(text)
        accrued_interest = (
            parse_pictet_amount(interest_match.group(2)) if interest_match else None
        )

        narration = (find_headline(text) or "Pictet bond purchase")[:140]

        return [
            Transaction(
                trade_date=parse_pictet_date(trade_date_raw),
                settlement_date=(
                    parse_pictet_date(value_date_raw) if value_date_raw else None
                ),
                booking_date=(
                    parse_pictet_date(booking_date_raw)
                    if booking_date_raw
                    else None
                ),
                narration=narration,
                title="Buy bonds",
                currency=currency,
                amount=amount,
                isin=resolve_isin(text),
                quantity=quantity,
                price=price,
                security_currency=nominal_currency,
                fees=fees,
                fees_currency=fees_currency,
                accrued_interest=accrued_interest,
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
