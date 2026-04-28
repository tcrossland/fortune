"""``pictet.payment.v1`` — outgoing third-party payment advice.

Pictet emits this document under ``PAYMENT TRANSACTIONS / Payment`` when
the client wires money to an external account. The advice carries:

  - the destination details (``Beneficiary``, ``Bank``, destination IBAN),
  - a free-text ``Communication`` field (the wire memo),
  - and the cash leg as ``Net amount`` under ``CASH EFFECT`` — already
    inclusive of any ``Payment fees`` Pictet charged on the wire.

We map the advice to a single
:class:`~banking_pipeline.models.Transaction` populating the fields the
writer's ``_render_third_party_payment`` path uses (``title``,
``transaction_number``, ``booking_date``); the cash leg's negative
sign keys the renderer to the ``Expenses:<prefix>:Other`` outgoing
counter-leg shape. Splitting the principal from the fees is a future
refactor — the document carries everything we'd need (``Gross amount``
and ``Payment fees`` both appear), but the placeholder elastic
counter-leg absorbs the combined debit so the entry balances.

Narration combines the beneficiary and the wire's communication, e.g.
``FIRST MIDDLE LASTNAMES - Liquidity``.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)


@dataclass
class PictetPaymentTemplate:
    template_id: str = "pictet.payment.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Beneficiary`` is the load-bearing field that distinguishes an
        # outgoing payment from an incoming one (``Instructing party``);
        # absence means the doc was misrouted.
        beneficiary = find_field(text, "Beneficiary")
        if beneficiary is None:
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
        communication = find_field(text, "Communication")

        if beneficiary and communication:
            narration = f"{beneficiary} - {communication}"
        else:
            narration = communication or beneficiary or "Pictet payment"

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
                title="Payment",
                currency=currency,
                amount=amount,
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
