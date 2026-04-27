"""Tests for ``pictet.reembolso_final.v1``.

Pictet ES structured-product maturity payouts share the
security-event banner with dividend notices but use ``Cantidad`` /
``Precio de rembolso`` (note Pictet's typo: missing second ``e``)
instead of the trade-advice labels. This module pins the field-level
extraction; a golden-file render test will land separately when a
``.beancount`` golden is added for this fixture.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetReembolsoFinalTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_reembolso_final_template_is_registered() -> None:
    assert "pictet.reembolso_final.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.reembolso_final.v1"]
    assert template.template_id == "pictet.reembolso_final.v1"


def test_reembolso_final_classifier_routes_to_reembolso_final() -> None:
    """Lock in classifier routing — distinct from the regular REEMBOLSO
    rule, which would only hit two of its patterns on this fixture
    (``Reembolso``, ``SALIDA de la cartera``) and score at ~0.70."""

    classification = LayeredClassifier().classify(_load("reembolso_final.txt"))
    assert classification.document_type is DocumentType.REEMBOLSO_FINAL
    assert classification.confidence > 0.90, (
        f"Expected confidence above 0.90 on the reembolso_final fixture; "
        f"got {classification.confidence:.3f}"
    )


def test_reembolso_final_extracts_single_transaction() -> None:
    template = PictetReembolsoFinalTemplate()
    txs = template.extract(_load("reembolso_final.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Dates: Pictet stamps trade/value/booking the same on this advice.
    assert tx.trade_date == date(2022, 6, 3)
    assert tx.settlement_date == date(2022, 6, 3)
    assert tx.booking_date == date(2022, 6, 3)
    # Cash leg is positive (proceeds in) — Pictet preserves signs as
    # printed; this is the convention every other security advice
    # follows.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("17444.16")
    # Security-event quantity is printed signed negative — units leaving.
    assert tx.quantity == Decimal("-604")
    # ``Precio de rembolso EUR 28.881061`` — note Pictet's typo in the
    # source label; the template's regex is tolerant of both spellings.
    assert tx.price == Decimal("28.881061")
    assert tx.security_currency == "EUR"
    assert tx.isin == "CH0559896136"
    # Narration composed from the ``Reembolso - <fund>`` security-event
    # line, mirroring the EN ``Redemption - <fund>`` form.
    assert tx.narration == "Reembolso - EUR PWM LG VOL BALANC (PICTET)21/22"
    assert tx.title == "Reembolso final"
    assert tx.transaction_number == "783667101"
    # IBAN won't validate against the anonymised checksum in the
    # fixture, so resolve_account_number falls back to the portfolio
    # identifier.
    assert tx.account_number == "K-123456.001"


def test_reembolso_final_template_rejects_regular_reembolso_doc() -> None:
    """The regular ``Reembolso`` advice (BOLSA DE VALORES / Reembolso)
    has ``Reembolso`` standalone but no ``final`` qualifier — the
    title-gate must reject it so the classifier can route to the
    correct template."""

    template = PictetReembolsoFinalTemplate()
    txs = template.extract(_load("reembolso.txt"))
    assert txs == []
