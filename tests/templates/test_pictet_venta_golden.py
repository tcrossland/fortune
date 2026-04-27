"""Golden-file test for the FX-and-fee-breakdown stock-sale render.

This is the first fixture that exercises three writer features at
once: the FX cash-leg ``@@ <subtotal> <ccy>`` annotation, the
sell-side ``{} @ <price>`` cost-basis form, AND a multi-item fee
breakdown rendered as one ``Expenses:<prefix>:Fees:<ccy>`` posting
per item with inline description comments. Pins the entry shape
established by ``venta.beancount``:

  - Inline ``open Income:<prefix>:<ISIN>`` directive at the top
    (first realized event for the position).
  - Asset leg first (sell-from-inventory at market).
  - One expense leg per fee component with ``; <description>`` comment.
  - FX-aware cash leg with ``@@`` total-cost annotation.
  - Elastic ``Income:<prefix>:<ISIN>`` posting (no ``:Realized``
    suffix — distinct from the simpler sell-without-breakdown shape
    used by ``reembolso_final``).
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
from banking_pipeline.templates.pictet import PictetVentaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_venta_renders_to_golden_beancount() -> None:
    txs = PictetVentaTemplate().extract(_load("venta.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the venta fixture"

    classification = Classification(
        document_type=DocumentType.VENTA,
        confidence=0.95,
        source="rules",
        template_id="pictet.venta.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "venta.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Venta entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
