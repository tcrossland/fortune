"""Tests for ``pictet.venta.v1``.

Sell counterpart to ``compra``. Pictet ES stock-exchange sale advice,
with FX cash-effect block and a multi-item fee breakdown
(``Corretaje y/o spread`` + ``Tasa bursátil``). Pins the field-level
extraction; the render shape is exercised by the matching golden test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetVentaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_venta_template_is_registered() -> None:
    assert "pictet.venta.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.venta.v1"]
    assert template.template_id == "pictet.venta.v1"


def test_venta_classifier_routes_to_venta() -> None:
    """Lock in classifier routing — distinct from REEMBOLSO, which would
    score 4/5 on this fixture (BOLSA + Tipo de op Venta + SALIDA +
    cantidad ejecutada all hit, only ``Reembolso`` misses) at ~0.91
    confidence and would otherwise mis-route the document to the fund-
    redemption template. The new VENTA rule scores 5/5 on its own
    fixture (the standalone ``Venta`` title is the discriminator)."""

    classification = LayeredClassifier().classify(_load("venta.txt"))
    assert classification.document_type is DocumentType.VENTA
    assert classification.confidence > 0.90, (
        f"Expected VENTA confidence above 0.90 on the fixture; "
        f"got {classification.confidence:.3f}"
    )


def test_venta_extracts_single_transaction() -> None:
    template = PictetVentaTemplate()
    txs = template.extract(_load("venta.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2022, 6, 8)
    assert tx.settlement_date == date(2022, 6, 10)
    assert tx.booking_date == date(2022, 6, 8)
    # Cash-leg currency is EUR (the client's account currency); security
    # currency is USD (the asset's quotation currency).
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("19593.05")
    assert tx.security_currency == "USD"
    # Quantity printed signed-negative (units leaving the portfolio).
    assert tx.quantity == Decimal("-119")
    assert tx.price == Decimal("178.6699")
    assert tx.isin == "IE00B579F325"
    # FX bridge fields populated from the EFECTO CASH block.
    assert tx.subtotal_security == Decimal("21047.84")
    assert tx.exchange_rate == Decimal("1.07425031")
    assert tx.is_fx is True
    # Aggregate fees from the inline ``Costes USD -213.88`` line.
    assert tx.fees == Decimal("-213.88")
    assert tx.fees_currency == "USD"
    # Per-line fee breakdown — the writer's sell-with-breakdown path
    # renders one expense leg per item with the description as an
    # inline beancount comment.
    assert len(tx.fee_breakdown) == 2
    descriptions = [item.description for item in tx.fee_breakdown]
    assert descriptions == ["Corretaje y/o spread", "Tasa bursátil"]
    assert tx.fee_breakdown[0].amount == Decimal("-212.54")
    assert tx.fee_breakdown[0].currency == "USD"
    assert tx.fee_breakdown[1].amount == Decimal("-1.34")
    # Headline narration extracted via ``find_headline`` — Pictet prints
    # the negative quantity inline.
    assert tx.narration == (
        "Venta -119 PHYSICAL GOLD (INVESCO) -ETC- PERP a USD 178.6699"
    )
    assert tx.title == "Venta"
    assert tx.transaction_number == "785210359"
    assert tx.account_number == "K-123456.001"


def test_venta_template_rejects_compra_doc() -> None:
    """The ``Compra`` advice is structurally identical except for
    direction; the standalone-title check is the discriminator."""

    template = PictetVentaTemplate()
    txs = template.extract(_load("compra.txt"))
    assert txs == []


def test_venta_template_rejects_reembolso_doc() -> None:
    """``Reembolso`` (fund redemption) shares the ``BOLSA DE VALORES``
    banner and ``Tipo de operación Venta`` but uses ``Reembolso`` as
    its title — the venta title-gate must reject it."""

    template = PictetVentaTemplate()
    txs = template.extract(_load("reembolso.txt"))
    assert txs == []
