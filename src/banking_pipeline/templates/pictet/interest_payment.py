"""``pictet.interest_payment.v1`` — quarterly current-account interest advice.

Pictet emits this document under ``CURRENT ACCOUNT / Interest payment`` once
per quarter to credit (rare — the user has a positive cash balance) or
debit (typical — the user is overdrawn) the interest accrued over the
period. Like ``debit_of_fees`` there is no security context — only a
signed cash leg, the period over which interest accrued, and a
calculation-basis description.

We map the advice to a single :class:`~banking_pipeline.models.Transaction`.
Narration is ``"Period dd.mm.yyyy - dd.mm.yyyy"`` derived from the
``Period`` field; title is the canonical ``"Interest payment"``. The
sign on ``amount`` flows through unchanged (negative for charges,
positive for earnings) — the writer's ``_render_interest`` path keys
the counter-leg account family (``Expenses:...:Interest`` vs
``Income:...:Interest``) on the sign.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_field,
    find_period,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)


def _format_pictet_date(d) -> str:  # noqa: ANN001 — local helper, datetime.date
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


@dataclass
class PictetInterestPaymentTemplate:
    template_id: str = "pictet.interest_payment.v1"

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

        # Narration: ``Period <range>`` formatted in Pictet's printed
        # dd.mm.yyyy form, mirroring the convention the fee-advice path
        # uses (``Periodo`` in ES, ``Period`` here in EN).
        period = find_period(text)
        if period:
            start, end = period
            narration = (
                f"Period {_format_pictet_date(start)} - {_format_pictet_date(end)}"
            )
        else:
            narration = "Pictet interest payment"

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
                title="Interest payment",
                currency=currency,
                amount=amount,
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
