"""Tests for ``pictet.transferencia_interna.v1``.

Spanish-locale cross-currency book transfer — counterpart to the EN
``INTERNAL_TRANSFER``. Pictet's Madrid branch issues this under
``TRÁFICO DE PAGOS / TRANSFERENCIA INTERNA DE EFECTIVO`` with two
``EFECTO CASH`` legs and an in-block ``Tipo de cambio`` FX rate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetTransferenciaInternaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_transferencia_interna_template_is_registered() -> None:
    assert "pictet.transferencia_interna.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.transferencia_interna.v1"]
    assert template.template_id == "pictet.transferencia_interna.v1"


def test_transferencia_interna_classifier_routes_correctly() -> None:
    """Lock in classifier routing — distinct from the other
    ``TRÁFICO DE PAGOS`` family members (``PAGO`` / ``PAGO ENTRANTE`` /
    ``Pago entrante``). The unique title plus the FX-bridge fields
    (``Subtotal`` + ``Tipo de cambio``) push this rule to a clean win."""

    classification = LayeredClassifier().classify(
        _load("transferencia_interna.txt")
    )
    assert classification.document_type is DocumentType.TRANSFERENCIA_INTERNA
    assert classification.confidence > 0.90, (
        f"Expected confidence above 0.90 on the transferencia_interna "
        f"fixture; got {classification.confidence:.3f}"
    )


def test_transferencia_interna_extracts_single_cross_currency_transaction() -> None:
    """Same single-Transaction shape as the EN sibling: ``currency`` /
    ``amount`` carry the source (debit) leg, ``counter_currency`` /
    ``counter_amount`` carry the destination (credit) leg, and the
    writer's ``render_internal_transfer`` builder emits one beancount
    entry with an ``@@`` annotation linking the two cash currencies."""

    template = PictetTransferenciaInternaTemplate()
    txs = template.extract(_load("transferencia_interna.txt"))

    assert len(txs) == 1
    tx = txs[0]

    assert tx.trade_date == date(2021, 11, 10)
    assert tx.settlement_date == date(2021, 11, 11)
    assert tx.booking_date == date(2021, 11, 10)

    # Source (debit) leg — the EUR account is debited 53,104.86 EUR.
    # Pictet's PDF signs this leg negative; the helper preserves it.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-53104.86")

    # Destination (credit) leg — the USD account is credited 61,188 USD.
    # Pictet performs the FX inside the source leg's CASH EFFECT block,
    # so the destination leg's Net amount is already in the destination
    # currency and signed positive.
    assert tx.counter_currency == "USD"
    assert tx.counter_amount == Decimal("61188.00")

    # FX rate quoted on the source leg as ``Tipo de cambio (EUR/USD)``.
    assert tx.exchange_rate == Decimal("1.15221084")

    # Synthesised narration — the document carries no verb-led headline,
    # so the template builds one from the leg pair.
    assert tx.narration == "EUR → USD"
    assert tx.title == "Transferencia interna de efectivo"
    assert tx.transaction_number == "733812025"
    # IBAN won't validate against the anonymised checksum in the
    # fixture, so resolve_account_number falls back to the portfolio
    # identifier.
    assert tx.account_number == "K-123456.001"


def test_transferencia_interna_template_rejects_pago_doc() -> None:
    """A regular ``Pago`` advice shares the ``TRÁFICO DE PAGOS`` banner
    but lacks the ``TRANSFERENCIA INTERNA DE EFECTIVO`` title; the
    template must bail rather than misinterpret a single-leg payment as
    a cross-currency book transfer."""

    template = PictetTransferenciaInternaTemplate()
    txs = template.extract(_load("pago_entrante.txt"))
    assert txs == []
