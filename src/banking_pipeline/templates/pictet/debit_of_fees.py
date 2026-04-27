"""``pictet.debit_of_fees.v1`` — quarterly fee debit advice.

Pictet emits this document under the standalone ``FEES / Debit of fees``
banner when administration / account-maintenance fees are billed against a
current account. There is no security context (no ISIN, no quantity, no
price) — just a signed cash leg, the period the fees cover, and a free-form
``Comment`` line that names the billing window.

We map the advice to a single :class:`~banking_pipeline.models.Transaction`
using the CASH EFFECT block's ``Net amount``. Narration prefers the
``Comment`` line (``Flat fees 1st quarter 2026``); when that's absent we
synthesise one from the ``Period`` field so the output is still useful for
auditing.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_comment_line,
    find_field,
    find_period,
    parse_pictet_date,
    resolve_account_number,
)


@dataclass
class PictetDebitOfFeesTemplate:
    template_id: str = "pictet.debit_of_fees.v1"

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

        comment = find_comment_line(text)
        period = find_period(text)
        if comment:
            narration = f"Pictet fees - {comment}"
        elif period:
            start, end = period
            narration = f"Pictet fees {start.isoformat()} to {end.isoformat()}"
        else:
            narration = "Pictet debit of fees"

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
