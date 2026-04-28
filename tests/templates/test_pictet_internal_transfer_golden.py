"""Golden-file test for the internal-money-transfer render.

Pictet ``Internal money transfer`` advices record a cross-currency
book transfer between the user's own current accounts as two
``CASH EFFECT`` blocks (one per leg). The new render shape emits a
single beancount entry with the destination leg carrying an
``@@ <abs_source> <src_ccy>`` annotation — distinct from the legacy
two-entry-balanced-against-Equity:Uncategorized shape produced by
the older ``_FX_LEG_TEMPLATE`` path.

Pins:

  - Two-string narration: canonical title + ``<src_ccy> → <dst_ccy>``.
  - Source-currency debit leg first (signed negative).
  - Destination-currency credit leg second with ``@@ <abs_source> <src_ccy>``
    annotation linking the two cash currencies for beancount's
    cross-reconciliation.
  - Trailing ``no:`` reference comment.

The fixture is fully anonymised, so the rendered values look uniform
(``99999.99`` everywhere). On a real document the values would diverge
across currencies, and the ``@@`` annotation would carry the actual
post-conversion source-currency total. Once a de-anonymised fixture
lands the golden updates with realistic numbers; the writer's contract
stays the same.
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
from banking_pipeline.templates.pictet import PictetInternalTransferTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_internal_transfer_renders_to_golden_beancount() -> None:
    txs = PictetInternalTransferTemplate().extract(_load("internal_transfer.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.INTERNAL_TRANSFER,
        confidence=0.95,
        source="rules",
        template_id="pictet.internal_transfer.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "internal_transfer.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Internal money transfer entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
