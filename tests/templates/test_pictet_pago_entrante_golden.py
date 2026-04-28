"""Golden-file test for the third-party incoming-payment render.

Pins the placeholder shape established by ``pago_entrante.beancount``:

  - Two-string narration: canonical title + ``<Ordenante> - <Comentario>``.
  - Cash leg credited to ``Assets:<prefix>:<currency>`` at the booking
    date, signed positive (cash in).
  - Elastic ``Income:<prefix>:Other`` posting that beancount auto-balances.
  - Trailing ``no:`` reference comment.

The ``Income:Pic:Other`` account is a deliberate placeholder — the
user can rewire to payer-specific income accounts (Earnout, Salary,
etc.) by extending ``_render_third_party_payment`` once the desired
chart-of-accounts convention is firmed up.
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
from banking_pipeline.templates.pictet import PictetPagoEntranteTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_pago_entrante_renders_to_golden_beancount() -> None:
    txs = PictetPagoEntranteTemplate().extract(_load("pago_entrante.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.PAGO_ENTRANTE,
        confidence=0.95,
        source="rules",
        template_id="pictet.pago_entrante.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "pago_entrante.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Pago entrante entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
