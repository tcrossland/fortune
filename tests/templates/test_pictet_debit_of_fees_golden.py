"""Golden-file test for the EN ``Debit of fees`` render.

EN counterpart to ``test_pictet_debito_de_gastos_golden``. Uses the
same ``_render_fee_advice`` builder; the fee-breakdown helper handles
both single-line and multi-line label layouts (the EN fixture wraps
``Administration flat fee (subject to VAT)`` across three source
lines, which the helper joins with single spaces).

Pins:

  - Booking-date entry date.
  - Two-string narration: canonical title + ``Period <range>`` in
    Pictet's printed dd.mm.yyyy form.
  - One ``Expenses:<prefix>:Fees:<ccy>`` posting per breakdown item
    with ``; <description>`` inline comment, including the joined
    multi-line label for the wrapped first item.
  - ``Assets:<prefix>:<currency>`` cash leg signed-negative.
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
from banking_pipeline.templates.pictet import PictetDebitOfFeesTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_debit_of_fees_renders_to_golden_beancount() -> None:
    txs = PictetDebitOfFeesTemplate().extract(_load("debit_of_fees.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.DEBIT_OF_FEES,
        confidence=0.95,
        source="rules",
        template_id="pictet.debit_of_fees.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "debit_of_fees.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Debit of fees entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
