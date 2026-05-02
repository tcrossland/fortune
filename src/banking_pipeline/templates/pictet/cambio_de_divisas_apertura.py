"""``pictet.cambio_de_divisas_apertura.v1`` — ES FX forward opening advice.

Spanish counterpart to :mod:`fx_forward`. Pictet's Madrid branch emits
this document under ``MERCADO DE DIVISAS / Cambio de divisas a plazo
(apertura)`` when an FX forward is *opened* — the contract is booked
but no cash moves on the trade date (both ``EFECTO CASH`` blocks
deliberately carry zero amounts as a signal that the cash leg lands
at maturity, not now). The matching cash settlement is recorded by
the paired ``CAMBIO_DE_DIVISAS_CIERRE`` advice that fires at the
maturity date and references the same ``Número de contrato`` /
``ID de transacción único``.

To avoid double-counting we emit no beancount entry for the opening:
the ``CAMBIO_DE_DIVISAS_CIERRE`` advice is the canonical paper trail
for the cash exchange, and a zero-amount memo entry on this side
would clutter the ledger without capturing meaningful state. Same
precedent as :mod:`fx_forward` for the EN locale.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction


@dataclass
class PictetCambioDeDivisasAperturaTemplate:
    template_id: str = "pictet.cambio_de_divisas_apertura.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Intentionally empty: the matching cierre advice carries the
        # cash leg for this contract at maturity. Emitting zero-amount
        # postings here would just clutter the ledger.
        return []
