"""Golden-file test for the EN incoming-payment render.

EN counterpart to ``test_pictet_pago_entrante_golden``. Pictet emits
``PAYMENT TRANSACTIONS / Incoming payment`` when a third party credits
the client's account. The render shape is identical to the ES variant
since both go through ``_render_third_party_payment``:

  - Two-string narration: canonical title + ``<Instructing party> - <Comment>``.
  - Cash leg credited to ``Assets:<prefix>:<currency>`` signed positive.
  - Elastic ``Income:<prefix>:Other`` posting that beancount auto-balances.
  - Trailing ``no:`` reference comment.
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
from banking_pipeline.templates.pictet import PictetIncomingPaymentTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_incoming_payment_renders_to_golden_beancount() -> None:
    txs = PictetIncomingPaymentTemplate().extract(_load("incoming_payment.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.INCOMING_PAYMENT,
        confidence=0.95,
        source="rules",
        template_id="pictet.incoming_payment.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "incoming_payment.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Incoming payment entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
