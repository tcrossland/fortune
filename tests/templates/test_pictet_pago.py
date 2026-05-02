"""Unit tests for ``pictet.pago.v1`` — ES outgoing third-party payment."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetPagoTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_pago_template_is_registered() -> None:
    assert "pictet.pago.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.pago.v1"].template_id == "pictet.pago.v1"


def test_pago_extracts_single_transaction() -> None:
    template = PictetPagoTemplate()
    txs = template.extract(_load("pago.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.title == "Pago"
    assert tx.trade_date == date(2022, 10, 28)
    assert tx.settlement_date == date(2022, 10, 28)
    assert tx.booking_date == date(2022, 10, 28)
    assert tx.currency == "EUR"
    # Net amount = -gross - fees = -(666666.66 + 22.22) = -666688.88.
    assert tx.amount == Decimal("-666688.88")
    # Beneficiary + Comunicación combined into narration.
    assert "NOMBRE BENEFICIARIO" in tx.narration
    assert "X2420238V" in tx.narration
    # BANCO SANTANDER doesn't resolve in beneficiary_bank_map by
    # default, so this stays None — the third-party path fires.
    assert tx.counter_account is None
    # No counterparty_account_map entry for "NOMBRE BENEFICIARIO" by
    # default either — the elastic ``:Other`` shape will be the
    # writer's pick.
    assert tx.counterparty_account is None
    assert tx.account_number == "K-123456.001"
    assert tx.transaction_number == "819390728"


def test_pago_rejects_incoming_payment() -> None:
    """A pago_entrante advice (``Ordenante`` field, no ``Beneficiario``)
    should yield nothing — the ``Beneficiario`` guard is what protects
    against classifier mis-routes between the ES payment-direction
    variants."""

    template = PictetPagoTemplate()
    txs = template.extract(_load("pago_entrante.txt"))
    assert txs == []
