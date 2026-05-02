"""Golden-file tests for the ES-locale ``Cambio de divisas`` renders.

Spanish counterparts to ``test_pictet_settle_fx_forward_golden`` and
``test_pictet_internal_transfer_golden``. The three Pictet ES FX
advices come in three render shapes:

  - ``Cambio de divisas al contado`` — spot trade. Renders through the
    internal-transfer builder, identical to the EN ``SPOT`` shape:
    two cash legs, ``@@ <abs_source> <src_ccy>`` annotation on the
    destination leg.
  - ``Cambio de divisas a plazo (apertura)`` — forward opening. No
    output: this doctype lives in :data:`NO_EMIT_TYPES` because the
    paired cierre advice books the cash leg at maturity.
  - ``Cambio de divisas a plazo (cierre)`` — forward settlement.
    Renders through the fx-settlement builder: fee-bearing cash leg,
    ``Expenses:<prefix>:Spread:<ccy>`` posting carrying the absolute-
    value spread (the ES advice prints it as ``Spread <CCY>`` rather
    than ``Forward spread <CCY>`` like the EN sibling, but it maps to
    the same canonical ``Spread`` segment via :func:`fee_segment`),
    and the counter cash leg with ``@@ <abs_gross> <ccy>`` where
    ``gross = amount - fees`` in signed arithmetic.
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
    PictetCambioDeDivisasAperturaTemplate,
    PictetCambioDeDivisasCierreTemplate,
    PictetCambioDeDivisasTemplate,
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


def test_cambio_de_divisas_renders_to_golden_beancount() -> None:
    txs = PictetCambioDeDivisasTemplate().extract(_load("cambio_de_divisas.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = _classification(
        DocumentType.CAMBIO_DE_DIVISAS, "pictet.cambio_de_divisas.v1"
    )
    rendered = beancount_writer.render_entry(txs[0], classification)
    expected = (FIXTURES / "cambio_de_divisas.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == expected, (
        "Rendered Cambio de divisas (al contado) entry doesn't match "
        "the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{expected}"
    )


def test_cambio_de_divisas_apertura_emits_no_beancount_output() -> None:
    """Apertura advices live in :data:`NO_EMIT_TYPES` — the writer must
    return the empty string for the whole document, header and all,
    so the eventual concatenated ledger doesn't carry a hollow audit
    block where the paired cierre advice will book the cash leg."""

    txs = PictetCambioDeDivisasAperturaTemplate().extract(
        _load("cambio_de_divisas_apertura.txt")
    )
    assert txs == []

    # Even with a synthetic non-empty transaction list, ``render``
    # must short-circuit because the doctype is no-emit.
    classification = _classification(
        DocumentType.CAMBIO_DE_DIVISAS_APERTURA,
        "pictet.cambio_de_divisas_apertura.v1",
    )
    result = ExtractionResult(
        classification=classification,
        transactions=[],
        source_path=Path("cambio_de_divisas_apertura.txt"),
    )
    assert beancount_writer.render(result) == ""


def test_cambio_de_divisas_cierre_renders_to_golden_beancount() -> None:
    txs = PictetCambioDeDivisasCierreTemplate().extract(
        _load("cambio_de_divisas_cierre.txt")
    )
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = _classification(
        DocumentType.CAMBIO_DE_DIVISAS_CIERRE,
        "pictet.cambio_de_divisas_cierre.v1",
    )
    rendered = beancount_writer.render_entry(txs[0], classification)
    expected = (FIXTURES / "cambio_de_divisas_cierre.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == expected, (
        "Rendered Cambio de divisas a plazo (cierre) entry doesn't "
        "match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{expected}"
    )
