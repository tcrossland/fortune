"""``pictet.suscripcion.v1`` — Spanish-locale fund subscription advice.

Spanish counterpart to :mod:`subscription_notice`. Issued by Pictet's
Madrid branch under ``BOLSA DE VALORES / Suscripción`` when a fund
subscription executes (``Tipo de operación: Compra``).

The notable structural difference from the English advice is that the
``EFECTO CASH`` block carries an FX conversion: Pictet bills the
subscription in the fund's quotation currency (``Importe bruto`` /
``Costes`` / ``Subtotal`` in USD for a USD fund) and converts to the
client's reference currency (``Importe neto`` in EUR) inside the same
block. The shared :func:`extract_simple_trade_advice` helper picks up
``Importe neto`` directly, so ``Transaction.currency`` ends up as EUR —
the actual cash-impact currency on the client's current account.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    extract_simple_trade_advice,
)

# Case-insensitive: 2023+ advices print the title as ``Suscripción`` (mixed
# case), 2021-era advices print it as ``SUSCRIPCIÓN`` (all caps). No other
# line in the document is just this word, so loosening case is safe.
_SUSCRIPCION_TITLE_RE = re.compile(r"^Suscripci[oó]n\s*$", re.M | re.I)


@dataclass
class PictetSuscripcionTemplate:
    template_id: str = "pictet.suscripcion.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Title check distinguishes from ``Compra`` (stock purchase),
        # which shares ``Tipo de operación: Compra`` but carries
        # ``Compra`` as its standalone title instead of ``Suscripción``.
        if not _SUSCRIPCION_TITLE_RE.search(doc.text):
            return []

        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Compra",),
            fallback_narration="Pictet suscripción",
            labels=ES_LABELS,
        )
        return [tx] if tx else []
