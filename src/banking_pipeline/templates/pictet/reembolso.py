"""``pictet.reembolso.v1`` — Spanish-locale fund redemption advice.

Spanish counterpart to :mod:`redemption_notice`. Issued by Pictet's
Madrid branch under ``BOLSA DE VALORES / Reembolso`` when a fund
redemption executes (``Tipo de operación: Venta``).

Unlike :mod:`suscripcion`, the fixture's ``EFECTO CASH`` block does
*not* carry an in-leg FX — the fund is USD and the proceeds land in the
client's USD current account, so trade currency and cash-impact currency
match. Both shapes are handled by the shared trade-advice helper; if a
real reembolso doc carries an FX leg (USD fund → EUR account, say) the
helper would still pick up ``Importe neto`` in EUR and produce the
correct cash-leg currency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    extract_simple_trade_advice,
)

_REEMBOLSO_TITLE_RE = re.compile(r"^Reembolso\s*$", re.M)


@dataclass
class PictetReembolsoTemplate:
    template_id: str = "pictet.reembolso.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        if not _REEMBOLSO_TITLE_RE.search(doc.text):
            return []

        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Venta",),
            fallback_narration="Pictet reembolso",
            labels=ES_LABELS,
            title="Reembolso",
        )
        return [tx] if tx else []
