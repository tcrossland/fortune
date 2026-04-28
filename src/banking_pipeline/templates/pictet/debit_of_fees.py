"""``pictet.debit_of_fees.v1`` — quarterly fee debit advice (English).

The English-locale counterpart to :mod:`debito_de_gastos`. Pictet emits
this document under the standalone ``FEES / Debit of fees`` banner when
administration / account-maintenance fees are billed against a current
account. There is no security context — just a signed cash leg, the
period the fees cover, a per-line ``Costs`` breakdown, and a free-form
``Comment`` line that names the billing window.

Populates the new model surface (booking_date, title,
transaction_number, fee_breakdown) so the writer's fee-advice path
can render the multi-leg, bank-prefixed entry shape. The breakdown
helper handles this fixture's multi-line label wrapping (e.g.
``Administration flat fee`` + ``(subject to VAT)`` + ``GBP -3'427.14``
across three lines), joining the parts with single spaces.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_comment_line,
    find_fee_breakdown,
    find_field,
    find_period,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)


def _format_pictet_date(d) -> str:  # noqa: ANN001 — local helper, datetime.date
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


@dataclass
class PictetDebitOfFeesTemplate:
    template_id: str = "pictet.debit_of_fees.v1"

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

        # Narration: prefer the period (formatted dd.mm.yyyy to match
        # Pictet's print convention) over the free-form comment line.
        period = find_period(text)
        if period:
            start, end = period
            narration = (
                f"Period {_format_pictet_date(start)} - {_format_pictet_date(end)}"
            )
        else:
            comment = find_comment_line(text)
            narration = comment or "Pictet debit of fees"

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
                title="Debit of fees",
                currency=currency,
                amount=amount,
                fee_breakdown=find_fee_breakdown(
                    text, costs_label="Costs", total_label="Total"
                ),
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]
