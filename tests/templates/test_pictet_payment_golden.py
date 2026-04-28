"""Golden-file test for the EN outgoing-payment render.

Mirror of the incoming-payment golden, sign-flipped: cash leg signed
negative (cash leaving the user's account), elastic counter-leg uses
``Expenses:<prefix>:Other`` instead of ``Income:<prefix>:Other``.
The single ``_render_third_party_payment`` builder branches on the
cash-leg sign to pick the right account family.

The fixture has a fee component on the wire (``Payment fees GBP -43.40``)
so ``Net amount`` is the all-in cash impact (``-12043.40``). The
elastic counter-leg currently absorbs the combined principal + fees;
splitting the fees into a dedicated ``Expenses:<prefix>:Fees:<ccy>``
posting is a future refactor.
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


def test_payment_renders_to_golden_beancount() -> None:
    txs = PictetPaymentTemplate().extract(_load("payment.txt"))
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
    golden = (FIXTURES / "payment.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Payment entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
