"""Golden-file test for the 2026 outgoing-payment fixture.

Distinct from ``test_pictet_payment_golden`` (the 2026-01-09 fixture)
in two ways the new fixture exercises:

  - **EUR cash leg** (vs the original GBP fixture). Pictet's PDF text
    has ``Net amount EUR -12'015.00`` and the Pictet portfolio account
    is the EUR sub-account; both flow through the writer unchanged.
  - **Partial-name beneficiary**: the ``Beneficiary`` is
    ``First LASTNAMES`` while the account holder is
    ``FIRST MIDDLE LASTNAMES``. Strict name-equality detection (as
    the old payment template used) would route this through the
    elastic two-leg shape; the new bank-map-based detection
    recognises the destination is one of the user's own accounts
    (``REVOLUT BANK UAB`` resolves to ``Revolut``) and emits the
    three-leg self-to-self form regardless of name match.
"""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.pictet import PictetPaymentTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_payment_2026_renders_to_golden_beancount() -> None:
    txs = PictetPaymentTemplate().extract(_load("payment.2026.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.PAYMENT,
        confidence=0.95,
        source="rules",
        template_id="pictet.payment.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "payment.2026.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Payment 2026 entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
