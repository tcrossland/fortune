"""Golden-file test for the FX-aware switch_entrada render.

This is the paired leg of ``test_pictet_switch_salida_golden``. Notable
shape differences from the salida path:

  - **FX cash leg** — Pictet bills the underlying buy in the security
    currency (USD here) but posts the cost into the EUR Switch holding,
    so the cash leg carries an ``@@ <subtotal> <ccy>`` annotation.
  - **Standard cost-basis braces** ``{<price> <ccy>}`` (entrada is a
    buy, units enter inventory at purchase price), not the ``{} @``
    reduce-from-inventory form salida uses.
  - **No Unrealized leg** — gains aren't realised on the buy side; the
    salida leg already booked the gain/loss for the rotated position.
  - **Cross-leg link** — the ``^<id>`` link references the *salida's*
    transaction number, not this document's, so ``bean-query`` can
    retrieve both legs of the switch with one filter. The entrada
    document doesn't carry that reference, so the test sets
    ``link_id`` manually; in production a higher-level pairing pass
    would resolve it from a salida + entrada batch.
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

# The transaction number Pictet stamped on the *salida* leg. The entrada
# document doesn't reference it, so the test patches it onto the
# extracted ``Transaction`` to demonstrate the writer's link behaviour.
# See the module docstring's ``Cross-leg link`` paragraph for context.
_PAIRED_SALIDA_TXN = "722909778"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_switch_entrada_2021_renders_to_golden_beancount() -> None:
    txs = PictetSwitchEntradaTemplate().extract(_load("switch_entrada.2021.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the switch fixture"

    # Pair the entrada with the salida via ``link_id``. This is what a
    # future pipeline-level pairing layer would do automatically; the
    # test does it inline because the entrada document carries no
    # reference to the salida.
    paired = txs[0].model_copy(update={"link_id": _PAIRED_SALIDA_TXN})

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

    rendered = beancount_writer.render_entry(paired, classification)
    golden = (FIXTURES / "switch_entrada.2021.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == golden, (
        "Rendered switch_entrada entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
