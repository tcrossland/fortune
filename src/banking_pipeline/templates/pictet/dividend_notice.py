"""``pictet.dividend_notice.v1`` — distribution / ordinary dividend advice.

Pictet emits this document under ``SECURITY EVENT / Distribution`` when a
fund pays an income distribution. Field shape diverges from the trade
advices: there's no ``Executed quantity`` / ``Execution price`` pair —
instead a static ``Quantity held`` records the underlying position and an
``Income per unit`` records the per-share dividend. ``Trade date`` aligns
with ``Ex date`` and ``Value date`` aligns with ``Payment date``, so reusing
the trade-date / settlement-date fields keeps the model uniform.

Narration comes from the ``Dividend - <fund name>`` line that Pictet places
under the section banner — neither ``find_headline`` (which scans for
trade verbs) nor the generic comment block applies here.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_field,
    find_subject_line,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
    resolve_isin,
)


@dataclass
class PictetDividendNoticeTemplate:
    template_id: str = "pictet.dividend_notice.v1"

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
        quantity_raw = find_field(text, "Quantity held")
        income_match = find_amount_field(text, "Income per unit")

        subject = find_subject_line(text, "Dividend")
        narration = (
            f"Pictet dividend - {subject}" if subject else "Pictet dividend"
        )[:140]

        tx = Transaction(
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
            currency=currency,
            amount=amount,
            isin=resolve_isin(text),
            quantity=parse_pictet_amount(quantity_raw) if quantity_raw else None,
            price=income_match[1] if income_match else None,
            account_number=resolve_account_number(text),
            source_path=doc.path,
        )
        return [tx]
