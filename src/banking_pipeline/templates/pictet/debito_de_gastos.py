"""``pictet.debito_de_gastos.v1`` — Spanish-locale fee debit advice.

Spanish counterpart to :mod:`debit_of_fees`. Issued by Pictet's Madrid
branch under ``GASTOS / Débito de gastos`` for quarterly administration
and account-maintenance fees. Same shape as the English advice — single
``EFECTO CASH`` block carrying ``Importe neto`` (signed negative), no
security context — just translated field labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_comment_line,
    find_field,
    find_period,
    parse_pictet_date,
    resolve_account_number,
)

_DEBITO_TITLE_RE = re.compile(r"^D[eé]bito\s+de\s+gastos\s*$", re.M)


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

        comment = find_comment_line(text, label="Comentario")
        period = find_period(text, label="Período")
        if comment:
            narration = f"Pictet gastos - {comment}"
        elif period:
            start, end = period
            narration = f"Pictet gastos {start.isoformat()} a {end.isoformat()}"
        else:
            narration = "Pictet débito de gastos"

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
            account_number=resolve_account_number(text, ES_LABELS),
            source_path=doc.path,
        )
        return [tx]
