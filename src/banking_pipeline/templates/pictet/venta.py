"""``pictet.venta.v1`` — Spanish-locale stock-exchange sale advice.

Sell counterpart to :mod:`compra`. Issued by Pictet's Madrid branch
under ``BOLSA DE VALORES / Venta`` when an exchange-traded sale
executes (``Tipo de operación: Venta``).

Same field shape as :mod:`compra`, distinguished from it only by
direction:

  - the title (``Venta`` standalone vs ``Compra``);
  - ``Cantidad ejecutada`` printed signed-negative (units leaving);
  - ``SALIDA de la cartera`` instead of ``ENTRADA en la cartera``.

Like :mod:`compra`, the ``EFECTO CASH`` block does FX from the trade
currency to the client's reference currency, so ``Transaction.currency``
reflects the cash-leg currency rather than the trade currency. The
fee block carries the same stock-exchange breakdown
(``Corretaje y/o spread`` + ``Tasa bursátil``) that distinguishes
exchange-traded sales from fund redemptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    extract_simple_trade_advice,
)

# Standalone ``Venta`` title (line on its own). The headline line also
# starts with ``Venta`` but carries quantity + asset name + price after
# it, so it doesn't match this anchored regex.
#
# Case-insensitive: 2022-era advices print the title as ``VENTA`` (all
# caps); newer advices likely print it as ``Venta`` (mixed case),
# mirroring the pattern we saw with ``COMPRA`` / ``Compra``.
_VENTA_TITLE_RE = re.compile(r"^Venta\s*$", re.M | re.I)


@dataclass
class PictetVentaTemplate:
    template_id: str = "pictet.venta.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        if not _VENTA_TITLE_RE.search(doc.text):
            return []

        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Venta",),
            fallback_narration="Pictet venta",
            labels=ES_LABELS,
            title="Venta",
        )
        return [tx] if tx else []
