"""``pictet.incoming_payment.v1`` — incoming third-party wire advice.

The English-locale counterpart to :mod:`pago_entrante`. Pictet emits
this document under ``PAYMENT TRANSACTIONS / Incoming payment`` when
an external bank credits the client's account. The advice carries the
source details (``Instructing party``, sending bank, ``Bank clearing
no``, ``Payment reference``) and a free-form ``Comment`` block.

There are no fees on incoming wires (``Costs EUR 0.00``), so
``Net amount`` equals ``Gross amount`` and the cash leg is positive.
We extract a single :class:`~banking_pipeline.models.Transaction`
populating the fields the writer's ``_render_third_party_payment``
path uses (``title``, ``transaction_number``, ``booking_date``);
narration combines the instructing party with the comment line for
audit readability.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_comment_line,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
    resolve_counterparty,
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

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, EN_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)
        comment = find_comment_line(text)

        # Narration combines the instructing party with the comment
        # line. Same shape as ``pago_entrante``'s narration.
        if instructing_party and comment:
            narration = f"{instructing_party} - {comment}"
        else:
            narration = comment or instructing_party or "Pictet incoming payment"

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
                narration=narration[:140],
                title="Incoming payment",
                currency=currency,
                amount=amount,
                # ``Instructing party`` → mapped income-account segment
                # via ``settings.counterparty_account_map`` when the
                # name resolves; ``None`` otherwise. The writer routes
                # the elastic counter-leg to the mapped account in
                # place of the catch-all ``:Other`` placeholder.
                counterparty_account=resolve_counterparty(instructing_party),
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
