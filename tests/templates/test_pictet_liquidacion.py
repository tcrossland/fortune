from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import (
    PictetLiquidacionAvisoPrevioRecepcionTemplate,
    PictetLiquidacionRecepcionDeValoresTemplate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


# ---------------------------------------------------------------------------
# Pre-arrival notice — ``AVISO PREVIO - RECEPCIÓN DE VALORES``
# ---------------------------------------------------------------------------


def test_aviso_previo_template_is_registered() -> None:
    template_id = "pictet.liquidacion_aviso_previo_recepcion.v1"
    assert template_id in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY[template_id]
    assert template.template_id == template_id


def test_aviso_previo_emits_no_transactions() -> None:
    """The pre-arrival notice is informational only — the paired
    recepcion advice books the actual position. Mirrors the EN
    ``FX_FORWARD`` template, which is also a no-emit document."""

    template = PictetLiquidacionAvisoPrevioRecepcionTemplate()
    txs = template.extract(_load("liquidacion_aviso_previo_recepcion.txt"))
    assert txs == []


# ---------------------------------------------------------------------------
# Securities receipt — ``RECEPCIÓN DE VALORES (GRATUITA)``
# ---------------------------------------------------------------------------


def test_recepcion_de_valores_template_is_registered() -> None:
    template_id = "pictet.liquidacion_recepcion_de_valores.v1"
    assert template_id in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY[template_id]
    assert template.template_id == template_id


def test_recepcion_de_valores_extracts_position_with_cost_basis() -> None:
    """The recepcion advice books a securities position with cost
    basis at the transfer's market value. The Transaction holds:

      - ``isin`` and ``quantity`` for the position itself
      - ``currency``/``amount`` for the cost-basis total in EUR
        (signed negative — value flowing from the equity bucket
        into the asset account)
      - ``trade_date`` set to the ``Transferencia / Fecha`` line
        (the actual transfer date, distinct from Pictet's later
        booking date)
    """

    template = PictetLiquidacionRecepcionDeValoresTemplate()
    txs = template.extract(_load("liquidacion_recepcion_de_valores.txt"))

    assert len(txs) == 1
    tx = txs[0]

    # Trade date is the Transferencia / Fecha line — the date the
    # position physically moved from the originating custodian.
    # Booking date is one day later (Pictet's internal accounting).
    assert tx.trade_date == date(2021, 12, 14)
    assert tx.settlement_date == date(2021, 12, 14)
    assert tx.booking_date == date(2021, 12, 15)

    # Position arriving in the portfolio.
    assert tx.isin == "LU0128494944"
    assert tx.quantity == Decimal("319.20882")

    # Cost-basis total in EUR, signed negative (value leaving
    # equity, landing as an asset). The writer flips sign for the
    # asset leg's total-cost annotation.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-43690.08")

    assert tx.title == "Recepción de valores (gratuita)"
    # Narration synthesised from the fund-name line under the
    # ENTRADA block — the document carries no verb-led headline.
    assert tx.narration == "PICTET-ST MONEY MARKET EUR-I"
    assert tx.transaction_number == "742426196"
    # Anonymised IBAN doesn't validate; falls back to the portfolio
    # identifier.
    assert tx.account_number == "K-123456.001"


def test_recepcion_de_valores_template_rejects_aviso_previo() -> None:
    """The aviso-previo advice shares the ``LIQUIDACIÓN`` banner but
    uses a different title and lacks the ``Estimacion de
    transferencia`` block. The recepcion template must bail rather
    than try to book the announcement as a position."""

    template = PictetLiquidacionRecepcionDeValoresTemplate()
    txs = template.extract(_load("liquidacion_aviso_previo_recepcion.txt"))
    assert txs == []
