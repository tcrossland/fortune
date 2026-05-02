"""``pictet.cambio_de_divisas.v1`` — Spanish-locale FX spot trade.

Spanish counterpart to :mod:`spot`. Pictet's Madrid branch emits this
document under ``MERCADO DE DIVISAS / Cambio de divisas al contado``
for over-the-counter spot FX trades. The body shape mirrors the EN
``Spot`` advice: two ``EFECTO CASH`` blocks land per document — the
sold currency leaves one current account (signed negative), the
bought currency lands in another (signed positive), both within the
same Pictet portfolio. The trade ID still carries the ``SPOTLUX``
suffix the EN advices use, so the cross-locale shape is unmistakeable.

The two legs map to a *single* :class:`Transaction` with the source
(debit) leg on ``currency``/``amount`` (signed negative — cash out) and
the destination (credit) leg on ``counter_currency`` /
``counter_amount`` (signed positive — cash in). The writer's
``_render_internal_transfer`` builder then emits a single beancount
entry with an ``@@ <abs_source> <src_ccy>`` annotation on the
destination leg — same shape as ``SPOT`` and ``INTERNAL_TRANSFER``.

Narration is the trade headline (``Venta USD -314'751.92 contra GBP a
1.284784``); the execution rate stays embedded there rather than
becoming a separate field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_cash_effect_legs,
    find_field,
    find_headline,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)

# Title appears on its own line — distinguishes this advice from the
# forward-open / forward-cierre variants (which share the
# ``MERCADO DE DIVISAS`` banner but use ``a plazo`` titles instead).
_CAMBIO_DE_DIVISAS_TITLE_RE = re.compile(
    r"^Cambio\s+de\s+divisas\s+al\s+contado\s*$", re.M | re.I
)


@dataclass
class PictetCambioDeDivisasTemplate:
    template_id: str = "pictet.cambio_de_divisas.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _CAMBIO_DE_DIVISAS_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text, ES_LABELS)
        if len(legs) != 2:
            return []

        # Pictet prints the sold-currency leg first (negative amount,
        # cash out) and the bought-currency leg second (positive,
        # cash in). Sort defensively in case a future fixture inverts
        # the order so the renderer's debit→credit assumption holds.
        debit_leg = next((leg for leg in legs if leg.amount < 0), None)
        credit_leg = next((leg for leg in legs if leg.amount > 0), None)
        if debit_leg is None or credit_leg is None:
            return []

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)
        narration = (find_headline(text, ES_LABELS) or "Pictet cambio de divisas")[:140]

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
                title="Cambio de divisas al contado",
                currency=debit_leg.currency,
                amount=debit_leg.amount,
                counter_currency=credit_leg.currency,
                counter_amount=credit_leg.amount,
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]
