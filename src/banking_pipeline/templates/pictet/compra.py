"""``pictet.compra.v1`` — Spanish-locale stock-exchange purchase advice.

Issued by Pictet's Madrid branch under ``BOLSA DE VALORES / Compra``
when an exchange-traded purchase executes (``Tipo de operación: Compra``).

Same field shape as :mod:`suscripcion`, distinguished from it only by:

  - the title (``Compra`` standalone vs ``Suscripción``);
  - the ``Costes`` breakdown (``Corretaje y/o spread`` plus
    ``Tasa bursátil`` for stock trades, vs ``Spread`` alone for funds);
  - the ``Plaza bursátil`` (a real exchange like SIX Swiss Exchange,
    vs ``All Funds`` for fund subscriptions).

Like :mod:`suscripcion`, the ``EFECTO CASH`` block does FX from the
trade currency to the client's reference currency, so
``Transaction.currency`` reflects the cash-leg currency rather than the
trade currency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    extract_simple_trade_advice,
)

# Standalone ``Compra`` title (line on its own). The headline line also
# starts with ``Compra`` but carries quantity + asset name + price after
# it, so it doesn't match this anchored regex.
_COMPRA_TITLE_RE = re.compile(r"^Compra\s*$", re.M)


@dataclass
class PictetCompraTemplate:
    template_id: str = "pictet.compra.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        if not _COMPRA_TITLE_RE.search(doc.text):
            return []

        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Compra",),
            fallback_narration="Pictet compra",
            labels=ES_LABELS,
            title="Compra",
        )
        return [tx] if tx else []
