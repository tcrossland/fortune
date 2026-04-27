"""``pictet.debito_de_gastos.v1`` — Spanish-locale fee debit advice.

Spanish counterpart to :mod:`debit_of_fees`. Issued by Pictet's Madrid
branch under ``GASTOS / Débito de gastos`` for quarterly administration
and account-maintenance fees. The advice has no security context (no
ISIN, no quantity, no price) — just a per-line breakdown of fee
components inside the ``Costes`` block, an aggregated cash leg in the
``EFECTO CASH`` block, and a free-form period the fees cover.

Populates the new model surface (booking_date, title, transaction_number,
fee_breakdown) so the writer's fee-advice path can render the
multi-leg, bank-prefixed entry shape the project's golden files use.
The legacy ``Comentario`` fallback is preserved for advices that lack
a ``Período`` line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_comment_line,
    find_fee_breakdown,
    find_field,
    find_period,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)

_DEBITO_TITLE_RE = re.compile(r"^D[eé]bito\s+de\s+gastos\s*$", re.M | re.I)


def _format_pictet_date(d) -> str:  # noqa: ANN001 — local helper, datetime.date
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


@dataclass
class PictetDebitoDeGastosTemplate:
    template_id: str = "pictet.debito_de_gastos.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _DEBITO_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, ES_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)

        # Narration: prefer the period (formatted dd.mm.yyyy to match
        # Pictet's print convention) over the free-form comment line.
        # Fall back to a bland string if neither is present.
        period = find_period(text, label="Período")
        if period:
            start, end = period
            narration = (
                f"Periodo {_format_pictet_date(start)} - {_format_pictet_date(end)}"
            )
        else:
            comment = find_comment_line(text, label="Comentario")
            narration = comment or "Pictet débito de gastos"

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
                title="Débito de gastos",
                currency=currency,
                amount=amount,
                fee_breakdown=find_fee_breakdown(
                    text, costs_label="Costes", total_label="Total"
                ),
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]
