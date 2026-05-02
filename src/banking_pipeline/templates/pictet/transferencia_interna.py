"""``pictet.transferencia_interna.v1`` — Spanish-locale cross-currency book transfer.

Spanish counterpart to :mod:`internal_transfer`. Pictet's Madrid branch
emits this document under ``TRÁFICO DE PAGOS / TRANSFERENCIA INTERNA
DE EFECTIVO`` when the client moves funds between their own current
accounts at Pictet across currencies. Two ``EFECTO CASH`` blocks land
per document, one per leg — the FX block (``Subtotal`` +
``Tipo de cambio`` + post-conversion ``Importe neto``) sits inside
whichever leg's gross/net currencies differ, mirroring the EN advice's
``Sub-total`` + ``Exchange rate`` shape.

Field-label divergences from the EN :mod:`internal_transfer` advice
------------------------------------------------------------------
The skeleton is identical; only the labels change. ``ES_LABELS`` already
covers every field the cash-effect helper reads
(``Importe neto`` / ``Subtotal`` / ``Tipo de cambio``), so the template
itself is barely more than the title gate plus the helper-driven body.

Both legs of the source fixture sit on the same Pictet portfolio
(``K-123456.001`` debited on the EUR sub-account, credited on the USD
sub-account). The single-Transaction shape with ``counter_currency`` /
``counter_amount`` lets the writer emit one beancount entry with an
``@@ <abs_source> <src_ccy>`` annotation linking the two cash
currencies — same render as the EN sibling, dispatched via
:data:`banking_pipeline.writer.builders.internal_transfer.INTERNAL_TRANSFER_TYPES`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_cash_effect_legs,
    find_exchange_rate,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)

# ``TRANSFERENCIA INTERNA DE EFECTIVO`` is the load-bearing tell that
# separates this advice from the other ``TRÁFICO DE PAGOS`` family
# members (``PAGO`` / ``PAGO ENTRANTE`` / ``Pago entrante``). Anchored
# to a full line via ``^...$`` + ``re.M``; ``re.I`` keeps the gate
# tolerant if Pictet ever ships a mixed-case variant.
_TRANSFERENCIA_INTERNA_TITLE_RE = re.compile(
    r"^TRANSFERENCIA\s+INTERNA\s+DE\s+EFECTIVO\s*$", re.M | re.I
)


@dataclass
class PictetTransferenciaInternaTemplate:
    template_id: str = "pictet.transferencia_interna.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _TRANSFERENCIA_INTERNA_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text, ES_LABELS)
        if len(legs) != 2:
            return []

        debit, credit = legs

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)

        # Synthesised narration — the document carries no verb-led
        # headline, so we lean on the leg pair to be self-describing.
        # ``→`` keeps the narration short and unambiguous; same shape
        # the EN sibling uses, since the arrow is locale-agnostic.
        narration = f"{debit.currency} → {credit.currency}"

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
                title="Transferencia interna de efectivo",
                currency=debit.currency,
                amount=debit.amount,
                counter_currency=credit.currency,
                counter_amount=credit.amount,
                exchange_rate=find_exchange_rate(text, ES_LABELS),
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]
