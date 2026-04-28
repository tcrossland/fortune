"""Tests for ``pictet.pago_entrante.v1``.

Third-party-payer counterpart to ``pago_interna``. The two share the
same ``TRÁFICO DE PAGOS`` banner and most field labels; the
discriminator is Pictet's title casing (``Pago entrante`` mixed case
here vs ``PAGO ENTRANTE`` all caps for the self-to-self variant).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetPagoEntranteTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_pago_entrante_template_is_registered() -> None:
    assert "pictet.pago_entrante.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.pago_entrante.v1"]
    assert template.template_id == "pictet.pago_entrante.v1"


def test_pago_entrante_classifier_routes_to_pago_entrante() -> None:
    """Lock in classifier routing — distinct from PAGO_INTERNA, which
    would otherwise score 4/5 on this fixture (every pattern except
    ``Referencia de pago``) at ~0.91 and mis-route the document. The
    case-sensitive title pattern is the load-bearing discriminator."""

    classification = LayeredClassifier().classify(_load("pago_entrante.txt"))
    assert classification.document_type is DocumentType.PAGO_ENTRANTE
    assert classification.confidence > 0.90, (
        f"Expected PAGO_ENTRANTE confidence above 0.90 on the fixture; "
        f"got {classification.confidence:.3f}"
    )


def test_pago_entrante_extracts_single_transaction() -> None:
    template = PictetPagoEntranteTemplate()
    txs = template.extract(_load("pago_entrante.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Dates: the fixture has matching trade/value/booking, all
    # de-anonymised to a parseable Pictet-format date.
    assert tx.trade_date == date(2022, 8, 15)
    assert tx.settlement_date == date(2022, 8, 15)
    assert tx.booking_date == date(2022, 8, 15)
    # Cash-in: signed positive (proceeds in).
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("9999999.99")
    # No security context for a payment advice.
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration combines Ordenante (third-party payer) with the
    # ``Comentario`` line. Title is the canonical doctype name.
    assert tx.narration == "SOME CORP - Commission"
    assert tx.title == "Pago entrante"
    assert tx.transaction_number == "999999999"
    assert tx.account_number == "K-999999.999"


def test_pago_entrante_template_rejects_pago_interna_doc() -> None:
    """``PAGO ENTRANTE`` (all caps — self-to-self variant) must be
    rejected by the third-party template; the case-sensitive title
    gate is what enforces the split."""

    template = PictetPagoEntranteTemplate()
    txs = template.extract(_load("pago_interna.txt"))
    assert txs == []
