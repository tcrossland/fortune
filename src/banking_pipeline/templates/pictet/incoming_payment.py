"""``pictet.incoming_payment.v1`` — incoming third-party wire advice.

The mirror of :mod:`payment`: Pictet emits this document under
``PAYMENT TRANSACTIONS / Incoming payment`` when an external bank credits
the client's account. The advice carries the source details
(``Instructing party``, sending bank, ``Bank clearing no``,
``Payment reference``) and a free-form ``Comment`` block.

There are no fees on incoming wires, so ``Net amount`` equals
``Gross amount`` and the cash leg is positive. We extract a single
:class:`~banking_pipeline.models.Transaction` with narration combining
the instructing party and the comment block.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_comment_line,
    find_field,
    parse_pictet_date,
    resolve_account_number,
)


@dataclass
class PictetIncomingPaymentTemplate:
    template_id: str = "pictet.incoming_payment.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Instructing party`` distinguishes incoming payments from outgoing
        # ones (which carry ``Beneficiary``); absence is the signal to bail.
        instructing_party = find_field(text, "Instructing party")
        if instructing_party is None:
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, "Net amount")
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, "Value date")
        comment = find_comment_line(text)

        narration_parts = [f"Pictet incoming payment from {instructing_party}"]
        if comment:
            narration_parts.append(comment)
        narration = " - ".join(narration_parts)[:140]

        tx = Transaction(
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
            currency=currency,
            amount=amount,
            isin=None,
            quantity=None,
            price=None,
            account_number=resolve_account_number(text),
            source_path=doc.path,
        )
        return [tx]
