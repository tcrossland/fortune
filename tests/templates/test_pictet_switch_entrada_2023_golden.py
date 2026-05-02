"""Golden-file test for the FX-bridged ``switch_entrada`` render with
a non-zero spread.

Distinct from ``test_pictet_switch_entrada_2023_golden`` (which has
``Costes 0.00`` and so emits no fees leg) on two fronts:

  - **FX bridge.** The underlying fund is in EUR but the source
    portfolio's CASH EFFECT amount is in USD — the document carries
    ``Subtotal EUR -17,450.12`` + ``Tipo de cambio (EUR/USD)`` +
    ``Importe neto USD -19,215.60``. The Switch holding leg ends up in
    USD with an ``@@ 17450.12 EUR`` annotation so beancount can
    reconcile the two cash currencies on a single entry.
  - **Spread fee.** The standalone ``Costes`` block lists ``Spread
    EUR 0.08`` — surfaces as a per-component
    ``Expenses:<prefix>:<portfolio>:Spread:EUR  0.08 EUR`` posting
    using the item description as the account segment (so a
    cross-year spread-cost query is a single account-prefix match,
    distinct from generic management / brokerage fees).

The PDF-extractor kerning artifact (``ENTRADAen`` with no inter-word
space — see line 43 of the fixture) is the same one the 2023 test
exercises.
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


def test_switch_entrada_202308_renders_to_golden_beancount() -> None:
    txs = PictetSwitchEntradaTemplate().extract(_load("switch_entrada.202308.txt"))
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
    golden = (FIXTURES / "switch_entrada.202308.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == golden, (
        "Rendered switch_entrada.202308 entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
