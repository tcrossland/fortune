"""``pictet.switch_entrada.v1`` — Spanish-locale fund-switch incoming leg.

Pictet emits this document under ``BOLSA DE VALORES /
Cambio ("switch") de fondos (entrada)`` as the *incoming* leg of a
two-document fund switch: the client redeems fund A (``switch_salida``)
and the proceeds buy fund B (``switch_entrada``). No external cash
moves — both legs are internal portfolio reorganisation — but Pictet
records each leg with a EUR cash-equivalent so the cost basis is
preserved.

Notable structural quirks vs :mod:`suscripcion`:

  - No ``Cuenta corriente`` line — the switch doesn't credit a client
    cash account, so there's no per-leg IBAN. ``account_number`` falls
    back to the portfolio header (``N° de cuenta``).
  - ``EFECTO CASH`` appears without a portfolio identifier after it,
    again because the cash never lands in a current account.

The shared :func:`extract_simple_trade_advice` helper handles both
quirks: ``find_amount_field`` picks up the unique ``Importe neto`` line
regardless of whether a ``CASH EFFECT`` portfolio marker follows, and
``resolve_account_number`` falls back to the portfolio header when no
IBAN is present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    extract_simple_trade_advice,
    find_switch_fund_name,
)

# Title line: ``Cambio ("switch") de fondos (entrada)``. Match the
# parenthesised ``(entrada)`` to distinguish from the salida sibling.
_SWITCH_ENTRADA_TITLE_RE = re.compile(r"Cambio.*\(entrada\)", re.I)


@dataclass
class PictetSwitchEntradaTemplate:
    template_id: str = "pictet.switch_entrada.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        if not _SWITCH_ENTRADA_TITLE_RE.search(doc.text):
            return []

        # Same narration shape as switch_salida — see that module's
        # comment. ``ENTRADA <fund>`` is the form the writer's switch
        # path expects on the entrada leg.
        fund = find_switch_fund_name(doc.text, "ENTRADA")
        narration = f"ENTRADA {fund}" if fund else "Pictet switch (entrada)"

        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Compra",),
            fallback_narration=narration,
            labels=ES_LABELS,
            title="Switch",
        )
        return [tx] if tx else []
