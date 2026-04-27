"""``pictet.payment.v1`` — outgoing third-party payment advice.

Pictet emits this document under ``PAYMENT TRANSACTIONS / Payment`` when
the client wires money to an external account. The advice carries:

  - the destination details (``Beneficiary``, ``Bank``, destination IBAN
    written without the Pictet ``IBAN?<…>`` shorthand),
  - a free-text ``Communication`` field (the wire memo),
  - and the cash leg as ``Net amount`` under ``CASH EFFECT`` — already
    inclusive of any ``Payment fees`` Pictet charged on the wire.

We map the advice to a single :class:`~banking_pipeline.models.Transaction`
using ``Net amount`` (so fee accounting is preserved as a single all-in
debit). Splitting the principal from the fees as separate transactions is
left to a future refactor — the document carries everything we'd need
(``Gross amount`` and ``Payment fees`` both appear), but the existing
single-event templates all return one transaction and we keep that shape
for now to stay consistent with subscription / redemption / dividend.

Narration combines the beneficiary and the wire's communication, e.g.
``Pictet payment to FIRST MIDDLE LASTNAMES - Liquidity``.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_field,
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

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, "Net amount")
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, "Value date")
        communication = find_field(text, "Communication")

        narration_parts = [f"Pictet payment to {beneficiary}"]
        if communication:
            narration_parts.append(communication)
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
