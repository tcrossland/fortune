"""``pictet.limit_extension.v1`` — credit-line extension advice.

Pictet emits this document under the standalone ``LIMIT / Extension``
banner when a Lombard / current-account credit facility is renewed or
extended. The advice records *no cash impact* — the ``CASH EFFECT`` block
explicitly carries ``Net amount = 0.00`` — but it does record an event
that's part of the audit trail (the limit was extended for another period,
typically with refreshed contract dates).

We emit a single zero-amount :class:`~banking_pipeline.models.Transaction`
so the event makes it into the pipeline's output. Downstream beancount
rendering can render it as a ``note`` directive (no postings), or as a
zero-amount ``txn`` for completeness — either is valid; the template's
job is just to capture *that* the event happened, with enough narration
to identify which limit and what period.

Narration source: the ``C/a limit <currency>, <period>`` line that Pictet
prints under ``REFERENCE HOLDING`` to describe the limit position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)

# ``C/a limit GBP, 26.02.2025-26.02.2026 - BP`` — the limit description.
_CA_LIMIT_RE = re.compile(r"^C/a\s+limit\s+(.+?)\s*$", re.M)


@dataclass
class PictetLimitExtensionTemplate:
    template_id: str = "pictet.limit_extension.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``C/a limit`` is unique to limit-extension advices and is the
        # field we lean on for narration; require it as a sanity check.
        ca_limit_match = _CA_LIMIT_RE.search(text)
        if ca_limit_match is None:
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, "Net amount")
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, "Value date")

        narration = f"Pictet limit extension - C/a limit {ca_limit_match.group(1)}"[:140]

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
            transaction_number=find_transaction_number(text),
            source_path=doc.path,
        )
        return [tx]
