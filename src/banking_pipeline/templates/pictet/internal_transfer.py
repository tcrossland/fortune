"""``pictet.internal_transfer.v1`` — cross-currency book transfer.

Pictet emits this document under ``PAYMENT TRANSACTIONS / Internal
money transfer`` when the client moves funds between their own current
accounts at Pictet, almost always crossing currencies (same-currency
moves get booked as a simple adjustment instead, not as a payment-
transactions advice). Two ``CASH EFFECT`` blocks land per document:

  - leg 1: source account, debited in the source currency
  - leg 2: destination account, credited in the destination currency.
    The in-block ``Exchange rate`` line records the FX, and the leg's
    ``Net amount`` is already in the post-conversion currency.

The two legs map to a *single* :class:`Transaction` here — source on
``currency``/``amount`` (signed negative — cash out), destination on
``counter_currency``/``counter_amount`` (signed positive — cash in).
The writer's ``_render_internal_transfer`` path uses both fields to
emit a single beancount entry with an ``@@ <abs_source> <src_ccy>``
annotation on the destination leg. Earlier this template produced two
separate ``Transaction`` rows that the writer rendered as two entries
balanced against ``Equity:Uncategorized``; the single-Transaction
shape lets beancount cross-reconcile the EUR↔GBP relationship the
document records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_cash_effect_legs,
    find_exchange_rate,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)

_INTERNAL_TRANSFER_TITLE_RE = re.compile(r"^Internal\s+money\s+transfer\s*$", re.M)


@dataclass
class PictetInternalTransferTemplate:
    template_id: str = "pictet.internal_transfer.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _INTERNAL_TRANSFER_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text, EN_LABELS)
        if len(legs) != 2:
            return []

        debit, credit = legs

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)

        # Synthesised narration — the document carries no verb-led
        # headline, so we lean on the leg pair to be self-describing.
        # ``→`` keeps the narration short and unambiguous.
        narration = f"{debit.currency} → {credit.currency}"

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
                title="Internal money transfer",
                currency=debit.currency,
                amount=debit.amount,
                counter_currency=credit.currency,
                counter_amount=credit.amount,
                exchange_rate=find_exchange_rate(text, EN_LABELS),
                # The IBAN extraction here picks up the source leg's IBAN
                # (it's the first ``Current account`` line in document
                # order); good enough for diagnostic purposes since the
                # writer keys postings on currency, not IBAN.
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
