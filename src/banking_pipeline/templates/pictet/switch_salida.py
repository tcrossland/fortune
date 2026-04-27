"""``pictet.switch_salida.v1`` — Spanish-locale fund-switch outgoing leg.

Pictet emits this document under ``BOLSA DE VALORES /
Cambio ("switch") de fondos (salida)`` as the *outgoing* leg of a
two-document fund switch (the incoming leg is :mod:`switch_entrada`).
Like its sibling, no external cash moves — both legs are internal
portfolio reorganisation — but Pictet records each with a EUR
cash-equivalent so the cost basis is preserved.

Same structural quirks as :mod:`switch_entrada` (no
``Cuenta corriente``, no portfolio identifier after ``EFECTO CASH``);
the shared trade-advice helper handles them by falling back to the
``N° de cuenta`` portfolio header when no IBAN is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    extract_simple_trade_advice,
)

_SWITCH_SALIDA_TITLE_RE = re.compile(r"Cambio.*\(salida\)", re.I)


@dataclass
class PictetSwitchSalidaTemplate:
    template_id: str = "pictet.switch_salida.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        if not _SWITCH_SALIDA_TITLE_RE.search(doc.text):
            return []

        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Venta",),
            fallback_narration="Pictet switch (salida)",
            labels=ES_LABELS,
        )
        return [tx] if tx else []
