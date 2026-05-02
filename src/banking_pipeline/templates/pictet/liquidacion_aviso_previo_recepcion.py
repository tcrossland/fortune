"""``pictet.liquidacion_aviso_previo_recepcion.v1`` — pre-arrival notice.

Pictet emits this document under ``LIQUIDACIÓN / AVISO PREVIO -
RECEPCIÓN DE VALORES`` when an external custodian announces an
incoming free-of-payment securities transfer. The advice lists the
positions Pictet expects to receive (one ``ENTRADA en la cartera``
block per lot — sometimes multiple lots of the same ISIN) but the
comment ``Un aviso seguirá a la recepción real de cada posición``
makes the contract explicit: this is informational, the booking
advice will follow per-lot.

To avoid double-counting we emit no beancount entry for the
pre-notice: the paired :mod:`liquidacion_recepcion_de_valores`
advice is the canonical paper trail for the position acquisition,
and a memo entry on this side would clutter the ledger without
capturing meaningful state. Same precedent as :mod:`fx_forward`
(vs ``SETTLE_FX_FORWARD``) and :mod:`cambio_de_divisas_apertura`
(vs ``CAMBIO_DE_DIVISAS_CIERRE``): when two documents describe the
same economic event, we book the position-acquiring one and skip
the announcement.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction


@dataclass
class PictetLiquidacionAvisoPrevioRecepcionTemplate:
    template_id: str = "pictet.liquidacion_aviso_previo_recepcion.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Intentionally empty: the matching ``RECEPCION_DE_VALORES``
        # advice carries the position booking. The aviso lists the
        # announced lots but at the time it's issued the actual cost
        # basis (``Estimacion de transferencia``) hasn't been
        # established — that's settled when the position physically
        # arrives and the cierre advice fires.
        return []
