"""Golden-file test for the FX-aware Pictet suscripción render.

Exercises the full extractor → writer path against
``tests/fixtures/es/pictet/suscripcion.fx.txt`` and asserts the rendered
entry matches ``tests/fixtures/es/pictet/suscripcion.fx.beancount``
character-for-character. The golden file is the canonical specification
for the FX entry format (booking-date entry date, two-string narration,
``Assets:<prefix>:<ISIN>`` cost-basis leg, broken-out ``Expenses:...:Fees``
leg, ``@@ <subtotal> <ccy>`` cash leg, trailing ``no:`` reference comment).

Add new ``<doctype>.<tag>.txt`` / ``<doctype>.<tag>.beancount`` pairs and
a corresponding test below to lock in additional render shapes; the
existing per-template extraction tests (``test_pictet_suscripcion`` etc.)
still cover the field-level contract.
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
from banking_pipeline.templates.pictet import PictetSuscripcionTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_suscripcion_fx_renders_to_golden_beancount() -> None:
    txs = PictetSuscripcionTemplate().extract(_load("suscripcion.fx.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the FX fixture"

    # The classifier would normally synthesise this; constructing it
    # inline keeps the test focused on the extractor + writer surface
    # without dragging the rule classifier in.
    classification = Classification(
        document_type=DocumentType.SUSCRIPCION,
        confidence=0.95,
        source="rules",
        template_id="pictet.suscripcion.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "suscripcion.fx.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered FX entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
