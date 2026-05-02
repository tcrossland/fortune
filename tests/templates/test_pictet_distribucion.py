"""Tests for ``pictet.distribucion.v1``.

Spanish-locale fund distribution / ordinary dividend — Madrid-branch
counterpart to the EN ``DIVIDEND_NOTICE``. Pictet emits this under
``HECHOS RELEVANTES / Distribución / Dividendo ordinario`` with
``Cantidad detenida`` (Quantity held) and ``Renta unitaria`` (Income
per unit) labels in place of the EN equivalents.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetDistribucionTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_distribucion_template_is_registered() -> None:
    assert "pictet.distribucion.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.distribucion.v1"]
    assert template.template_id == "pictet.distribucion.v1"


def test_distribucion_classifier_routes_to_distribucion() -> None:
    """Lock in classifier routing — distinct from REEMBOLSO_FINAL,
    which shares the ``HECHOS RELEVANTES`` banner but uses ``Reembolso
    final`` as its subtitle and ``Precio de rembolso`` as its
    load-bearing field."""

    classification = LayeredClassifier().classify(_load("distribucion.txt"))
    assert classification.document_type is DocumentType.DISTRIBUCION
    assert classification.confidence > 0.90, (
        f"Expected confidence above 0.90 on the distribucion fixture; "
        f"got {classification.confidence:.3f}"
    )


def test_distribucion_extracts_single_transaction() -> None:
    template = PictetDistribucionTemplate()
    txs = template.extract(_load("distribucion.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Trade date == ex date; settlement_date == payment date;
    # booking_date == when the cash actually moved (used as the entry
    # date by the writer). Mapping mirrors the EN dividend_notice.
    assert tx.trade_date == date(2022, 12, 1)
    assert tx.settlement_date == date(2022, 12, 8)
    assert tx.booking_date == date(2022, 12, 8)
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("2044.90")
    # Quantity held = the position that generated the dividend, not a
    # transferred quantity. 1'415.198 × 1.444955 ≈ 2044.90 (matches
    # Net amount).
    assert tx.quantity == Decimal("1415.198")
    assert tx.price == Decimal("1.444955")
    assert tx.isin == "IE00B8Y2XY28"
    # Narration uses the ``Dividendo - <fund>`` security-event subject
    # line, mirroring the EN ``Dividend - <fund>`` form.
    assert tx.narration == "Dividendo - MUZINICH-SUST.CREDIT S HGD EUR"
    assert tx.title == "Distribución"
    assert tx.transaction_number == "829677165"
    # IBAN won't validate against the anonymised checksum in the
    # fixture, so resolve_account_number falls back to the portfolio
    # identifier.
    assert tx.account_number == "K-123456.001"


def test_distribucion_template_rejects_reembolso_final_doc() -> None:
    """The reembolso_final advice shares the ``HECHOS RELEVANTES``
    banner but has ``Reembolso final`` as its subtitle, not
    ``Distribución`` — the title gate must reject it so the classifier
    can route to the correct template."""

    template = PictetDistribucionTemplate()
    txs = template.extract(_load("reembolso_final.txt"))
    assert txs == []
