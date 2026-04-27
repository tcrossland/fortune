"""``pictet.interest_scale.v1`` — quarterly per-day interest rate ledger.

Pictet emits this document under ``CURRENT ACCOUNT / Interest scale`` as a
companion to :mod:`interest_payment`. Where ``interest_payment`` carries
the cash leg, ``interest_scale`` shows the per-day balance / rate
breakdown that produced it. Both documents describe the *same* economic
event for the same account and period.

We extract a transaction here (using the ``Total`` row at the top of the
table) so users who only have the scale on hand still get the interest
data. If they have **both** the scale and the matching payment, the
pipeline will emit two transactions for the same event — deduplication is
left to the caller, since templates can't see across documents.

Field provenance is unusual on this document:
    - ``trade_date`` / ``settlement_date`` ← end of the ``Period`` line
      (the document carries no ``Trade date`` field of its own).
    - ``currency`` ← parenthetical on the ``BALANCE (XXX)`` column header.
    - ``amount`` ← the ``Total`` line at the top of the table.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_balance_currency,
    find_period,
    find_total_amount,
    resolve_account_number,
)


@dataclass
class PictetInterestScaleTemplate:
    template_id: str = "pictet.interest_scale.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        period = find_period(text)
        if period is None:
            return []
        start, end = period

        currency = find_balance_currency(text)
        if currency is None:
            return []

        amount = find_total_amount(text)
        if amount is None:
            return []

        narration = (
            f"Pictet interest scale {start.isoformat()} to {end.isoformat()}"
        )[:140]

        tx = Transaction(
            # Use the period end as the booking date — interest_scale
            # documents carry no Trade/Value date fields, but the table is
            # always settled on the period end (matches interest_payment's
            # Trade date, which the user can rely on for dedup).
            trade_date=end,
            settlement_date=end,
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
