"""Golden-file test for the quarterly interest-payment render.

Pins the bank-prefixed two-leg interest shape:

  - Booking-date entry date.
  - Two-string narration: canonical title (``"Interest payment"``) plus
    ``"Period dd.mm.yyyy - dd.mm.yyyy"`` range.
  - Counter-leg keyed on direction:
      * negative cash → ``Expenses:<prefix>:Interest:<ccy>`` (interest
        charged to the user on a debit balance — the typical case
        for an overdraft);
      * positive cash → ``Income:<prefix>:Interest:<ccy>`` (interest
        paid by Pictet on a credit balance — rare in practice).
  - ``Assets:<prefix>:<currency>`` cash leg signed as Pictet printed it.
  - Trailing ``no:`` reference comment.

The fixture is the debit-balance variant — the user is overdrawn in
GBP and pays interest. A future credit-balance fixture would land its
own golden with the ``Income:`` counter-leg, exercising the writer's
sign-keyed branching.
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
from banking_pipeline.templates.pictet import PictetInterestPaymentTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_interest_payment_renders_to_golden_beancount() -> None:
    txs = PictetInterestPaymentTemplate().extract(_load("interest_payment.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.INTEREST_PAYMENT,
        confidence=0.95,
        source="rules",
        template_id="pictet.interest_payment.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "interest_payment.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Interest payment entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
