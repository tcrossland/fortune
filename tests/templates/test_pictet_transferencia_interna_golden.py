"""Golden-file test for the Spanish-locale internal-money-transfer render.

Pins the same single-entry-with-``@@`` shape the EN sibling uses:

  - Booking-date entry date.
  - Two-string narration: ES title (``"Transferencia interna de
    efectivo"``) plus ``<src_ccy> → <dst_ccy>`` direction.
  - Source-currency debit leg first (signed negative).
  - Destination-currency credit leg second with
    ``@@ <abs_source> <src_ccy>`` annotation linking the two cash
    currencies for beancount's cross-reconciliation.
  - Trailing ``no:`` reference comment.

Routes through the same builder as ``INTERNAL_TRANSFER`` —
the source/destination leg shape is identical across locales; only
the title carries the ES vocabulary.
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
from banking_pipeline.templates.pictet import (
    PictetTransferenciaInternaTemplate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_transferencia_interna_renders_to_golden_beancount() -> None:
    txs = PictetTransferenciaInternaTemplate().extract(
        _load("transferencia_interna.txt")
    )
    assert len(txs) == 1, (
        "Expected exactly one transaction from the transferencia_interna fixture"
    )

    classification = Classification(
        document_type=DocumentType.TRANSFERENCIA_INTERNA,
        confidence=0.95,
        source="rules",
        template_id="pictet.transferencia_interna.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (
        FIXTURES / "transferencia_interna.beancount"
    ).read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Transferencia interna entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
