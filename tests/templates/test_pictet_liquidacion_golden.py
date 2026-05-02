"""Golden-file tests for the ES-locale ``LIQUIDACIÓN`` renders.

The two fixtures form a paired-advice family analogous to the
FX-forward apertura/cierre pair:

  - ``LIQUIDACION_AVISO_PREVIO_RECEPCION`` is no-emit (paper-trail
    only; the paired recepcion advice books the position) — the
    writer must return the empty string for the whole document.
  - ``LIQUIDACION_RECEPCION_DE_VALORES`` renders through the
    transfer-in builder: an asset leg with total-cost
    ``{{<total> <ccy>, <lot_date>}}`` annotation and an
    ``Equity:<prefix>:<portfolio>:Transfers`` offset leg. No cash
    leg — the receipt is free of payment.

Pins:

  - Two-string narration: ``"Recepción de valores (gratuita)"
    "<fund name>"``.
  - Asset leg first, with the ISIN as both the commodity column and
    the cost-basis currency (since beancount's commodity for a
    held security is its ISIN).
  - Lot date inside the cost-basis braces is the document's
    ``Transferencia / Fecha`` line (the actual transfer date),
    not the entry-header booking date.
  - Equity leg signed negative — value flowing from the equity
    bucket into the asset position.
  - Trailing ``no:`` reference.
"""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    ExtractionResult,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.pictet import (
    PictetLiquidacionAvisoPrevioRecepcionTemplate,
    PictetLiquidacionRecepcionDeValoresTemplate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def _classification(
    doc_type: DocumentType, template_id: str
) -> Classification:
    return Classification(
        document_type=doc_type,
        confidence=0.95,
        source="rules",
        template_id=template_id,
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )


def test_aviso_previo_emits_no_beancount_output() -> None:
    """The aviso-previo lives in :data:`NO_EMIT_TYPES` — even with a
    synthetic non-empty transaction list, ``render`` must return the
    empty string. The paired recepcion advice is the canonical
    paper trail for the actual position acquisition."""

    txs = PictetLiquidacionAvisoPrevioRecepcionTemplate().extract(
        _load("liquidacion_aviso_previo_recepcion.txt")
    )
    assert txs == []

    classification = _classification(
        DocumentType.LIQUIDACION_AVISO_PREVIO_RECEPCION,
        "pictet.liquidacion_aviso_previo_recepcion.v1",
    )
    result = ExtractionResult(
        classification=classification,
        transactions=[],
        source_path=Path("liquidacion_aviso_previo_recepcion.txt"),
    )
    assert beancount_writer.render(result) == ""


def test_recepcion_de_valores_renders_to_golden_beancount() -> None:
    txs = PictetLiquidacionRecepcionDeValoresTemplate().extract(
        _load("liquidacion_recepcion_de_valores.txt")
    )
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = _classification(
        DocumentType.LIQUIDACION_RECEPCION_DE_VALORES,
        "pictet.liquidacion_recepcion_de_valores.v1",
    )
    rendered = beancount_writer.render_entry(txs[0], classification)
    expected = (FIXTURES / "liquidacion_recepcion_de_valores.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == expected, (
        "Rendered Recepción de valores entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{expected}"
    )
