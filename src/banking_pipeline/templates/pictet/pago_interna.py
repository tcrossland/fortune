"""``pictet.pago_interna.v1`` — Spanish-locale incoming payment advice.

Spanish counterpart to :mod:`incoming_payment`. Issued by Pictet's
Madrid / Luxembourg-issued ES locale under ``TRÁFICO DE PAGOS / PAGO
ENTRANTE`` when an external bank credits the client's account.

Field shape mirrors :mod:`incoming_payment` with translated labels:

  - ``Ordenante`` (instructing party) — the load-bearing field that
    distinguishes incoming from outgoing payments;
  - ``Comentario`` block carries free-form context;
  - ``EFECTO CASH`` block (note: this fixture has the marker with a
    *space* before ``en la cartera``, unlike most ES advices which
    write ``EFECTO CASHen la cartera`` — the helper handles both
    via ``\\s*`` flex on the portfolio regex).

The advice is also flagged as ``PAGO INTERNA`` when the ordering party
matches the account holder (i.e. a self-to-self transfer from a
client-owned external account such as Revolut). That's a downstream
concern: this template just extracts the payment as-is, and the
reconciliation of self-to-self vs third-party is left to the beancount
writer.
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
    parse_pictet_date,
    resolve_account_number,
)

_PAGO_ENTRANTE_TITLE_RE = re.compile(r"^PAGO\s+ENTRANTE\s*$", re.M)


@dataclass
class PictetPagoInternaTemplate:
    template_id: str = "pictet.pago_interna.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _PAGO_ENTRANTE_TITLE_RE.search(text):
            return []

        ordenante = find_field(text, "Ordenante")
        if ordenante is None:
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

        narration_parts = [f"Pictet pago entrante de {ordenante}"]
        if comment:
            narration_parts.append(comment)
        narration = " - ".join(narration_parts)[:140]

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
            account_number=resolve_account_number(text, ES_LABELS),
            source_path=doc.path,
        )
        return [tx]
