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
trade verbs) nor the generic comment block applies here. Title is the
canonical ``"Dividend"``, mirroring the convention established by the
other security-event doctypes (``Reembolso final``, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_field,
    find_subject_line,
    find_transaction_number,
    find_withholding_tax,
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

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, EN_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)
        quantity_raw = find_field(text, "Quantity held")
        income_match = find_amount_field(text, "Income per unit")

        subject = find_subject_line(text, "Dividend")
        narration = (
            f"Dividend - {subject}" if subject else "Pictet dividend"
        )[:140]

        isin = resolve_isin(text)
        # Foreign withholding tax, when the advice carries it. ``amount``
        # (net) is unchanged; the writer splits the gross income leg from
        # the WHT leg.
        wht = find_withholding_tax(text, EN_LABELS, isin)

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
                title="Dividend",
                currency=currency,
                amount=amount,
                isin=isin,
                quantity=parse_pictet_amount(quantity_raw) if quantity_raw else None,
                price=income_match[1] if income_match else None,
                gross_income=wht[0] if wht else None,
                withholding_tax=wht[1] if wht else None,
                withholding_country=wht[2] if wht else None,
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
