"""``pictet.internal_transfer.v1`` — cross-currency book transfer.

Pictet emits this document under ``PAYMENT TRANSACTIONS / Internal money
transfer`` when the client moves funds between their own current accounts
at Pictet, almost always crossing currencies (same-currency moves get
booked as a simple adjustment instead, not as a payment-transactions
advice). Two ``CASH EFFECT`` blocks land per document:

  - leg 1: source account, debited in source currency
  - leg 2: destination account, credited in destination currency. The
    in-block ``Exchange rate`` line records the FX, and the leg's
    ``Net amount`` is already in the post-conversion currency.

Each leg becomes one :class:`Transaction`. The two share narration so
they're rejoinable as a single transfer event downstream. Narration is
synthesised from the leg currencies and amounts since the document
carries no verb-led headline of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_cash_effect_legs,
    find_field,
    legs_to_transactions,
    parse_pictet_date,
)

_INTERNAL_TRANSFER_TITLE_RE = re.compile(r"^Internal\s+money\s+transfer\s*$", re.M)


@dataclass
class PictetInternalTransferTemplate:
    template_id: str = "pictet.internal_transfer.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _INTERNAL_TRANSFER_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text)
        if len(legs) != 2:
            return []

        value_date_raw = find_field(text, "Value date")

        # No verb-led headline on internal transfers — synthesise one
        # from the leg pair so the narration is self-describing.
        debit, credit = legs
        narration = (
            f"Pictet internal transfer {debit.currency} {debit.amount} "
            f"to {credit.currency} {credit.amount}"
        )

        return legs_to_transactions(
            legs,
            doc=doc,
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
        )
