"""Golden-file test for the 2023-era ``switch_entrada`` render.

Distinct from ``test_pictet_switch_entrada_golden`` (the 2021 fixture)
in two structural ways:

  - The portfolio header in the document is ``ENTRADAen la cartera``
    with no whitespace between ``ENTRADA`` and ``en`` (Pictet's PDF
    extractor occasionally squishes the words together). The
    ``find_switch_fund_name`` helper accepts ``\\s*`` between the side
    and the preposition to tolerate this; older fixtures have the
    space.
  - No paired ``link_id`` is set on the entrada Transaction here, so
    the writer falls back to ``transaction_number`` for the ``^link``
    in the header — distinct from the 2021 fixture's test, which
    patches ``link_id`` to demonstrate the salida↔entrada pairing.
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
from banking_pipeline.templates.pictet import PictetSwitchEntradaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_switch_entrada_2023_renders_to_golden_beancount() -> None:
    txs = PictetSwitchEntradaTemplate().extract(_load("switch_entrada.2023.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.SWITCH_ENTRADA,
        confidence=0.95,
        source="rules",
        template_id="pictet.switch_entrada.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "switch_entrada.2023.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == golden, (
        "Rendered switch_entrada.2023 entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
