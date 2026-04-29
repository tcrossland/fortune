"""``pictet.sell_bonds.v1`` — bond sale advice.

Pictet emits this document under ``SECURITY / Sell bonds`` when a held
bond is sold before maturity. The advice shape differs from a fund
redemption / stock sale in three load-bearing ways:

  - Units are quoted as face value: ``Executed nominal EUR -90'000.00``
    (the regular trade advices use ``Executed quantity`` for unit
    count instead).
  - Price is a percentage of face value: ``Execution price 102.902%``.
    Per-unit price for beancount = percentage / 100.
  - The ``CASH EFFECT`` block carries an ``Interest`` line — accrued
    interest the buyer pays to the seller for the period since the
    last coupon. Net amount = gross + interest + costs.

We render this as a five-leg entry:

  - Cash leg (positive, the all-in proceeds).
  - Fees leg (commission expense).
  - Accrued-interest leg (income posted to
    ``Income:<prefix>:<isin>:Interest``, signed negative because
    income accounts are credited).
  - Asset leg (negative units leaving inventory at face value, with
    ``{} @ <unit_price>`` so beancount reduces the position at its
    cost basis and ``@`` records the sale price for capital-gains).
  - Elastic ``Income:<prefix>:<isin>:Realized`` leg that beancount
    auto-balances against the cost-vs-proceeds difference on the
    principal alone (the interest leg is excluded by the dedicated
    interest posting above).
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

# ``Execution price 102.902%`` — number with optional decimal, trailing %.
_PRICE_PCT_RE = re.compile(r"^Execution\s+price\s+(-?\d+(?:\.\d+)?)\s*%\s*$", re.M)

# ``Interest EUR 1'945.23`` — accrued interest line inside the CASH EFFECT
# block. Standard Pictet ``<label> <CCY> <amount>`` shape, but the label
# is unique to bond-sale advices among the trade family.
_INTEREST_RE = re.compile(
    r"^Interest\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$", re.M
)


@dataclass
class PictetSellBondsTemplate:
    template_id: str = "pictet.sell_bonds.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Operation type`` guard: bond advices share the SECURITY banner
        # with buy/sell of equities/funds; ``Sell`` is the load-bearing
        # discriminator from a bond *purchase* (which would also be a
        # ``Sell bonds`` candidate at some future point).
        op = find_field(text, "Operation type")
        if op != "Sell":
            return []

        # ``Executed nominal`` is the bond-specific quantity field. Bail if
        # missing — that's the structural marker of a bond advice and its
        # absence means the doc is misrouted.
        nominal_match = re.search(
            r"^Executed\s+nominal\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$",
            text,
            re.M,
        )
        if nominal_match is None:
            return []
        nominal_currency = nominal_match.group(1)
        quantity = parse_pictet_amount(nominal_match.group(2))

        # Percentage price; convert to per-face-unit decimal.
        price_match = _PRICE_PCT_RE.search(text)
        if price_match is None:
            return []
        price = parse_pictet_amount(price_match.group(1)) / Decimal("100")

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        # Cash impact is the ``Net amount`` line — Pictet's all-in figure
        # (gross + accrued interest - costs).
        cash_effect = find_amount_field(text, EN_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)

        # Costs (commission/fee) — the per-leg ``Costs <ccy> <amount>``
        # line in the CASH EFFECT block. Bond advices use
        # ``Commission/Fee`` as the breakdown label but Pictet still
        # writes the rolled-up total as ``Costs`` inside CASH EFFECT.
        costs_match = find_amount_field(text, EN_LABELS.costs)
        fees_currency = costs_match[0] if costs_match else None
        fees = costs_match[1] if costs_match else None

        # Accrued interest — bond-specific, sits between Gross and
        # Net inside CASH EFFECT.
        interest_match = _INTEREST_RE.search(text)
        accrued_interest = (
            parse_pictet_amount(interest_match.group(2)) if interest_match else None
        )

        narration = (find_headline(text) or "Pictet bond sale")[:140]

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
                title="Sell bonds",
                currency=currency,
                amount=amount,
                isin=resolve_isin(text),
                quantity=quantity,
                price=price,
                # Bond face-value units are tracked in the bond's own
                # currency; ``security_currency`` matches ``currency``
                # because Pictet's bond advices today are single-currency
                # (no FX leg).
                security_currency=nominal_currency,
                fees=fees,
                fees_currency=fees_currency,
                accrued_interest=accrued_interest,
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
