"""``pictet.interest_payment.v1`` — quarterly current-account interest advice.

Pictet emits this document under ``CURRENT ACCOUNT / Interest payment`` once
per quarter to credit (rare) or debit (typical, on overdrawn balances) the
interest accrued over the period. Like ``debit_of_fees`` there is no
security context — only a signed cash leg, the period over which interest
accrued, and a calculation-basis description.

We map the advice to a single :class:`~banking_pipeline.models.Transaction`.
Narration is synthesised from the ``Period`` field because the document
carries no headline-style summary line.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_field,
    find_period,
    parse_pictet_date,
    resolve_account_number,
)


@dataclass
class PictetInterestPaymentTemplate:
    template_id: str = "pictet.interest_payment.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, "Net amount")
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, "Value date")

        period = find_period(text)
        if period:
            start, end = period
            narration = f"Pictet interest {start.isoformat()} to {end.isoformat()}"
        else:
            narration = "Pictet interest payment"

        tx = Transaction(
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration[:140],
            currency=currency,
            amount=amount,
            isin=None,
            quantity=None,
            price=None,
            account_number=resolve_account_number(text),
            source_path=doc.path,
        )
        return [tx]
