"""Golden-file test for the ``Compra`` (Spanish stock-purchase) render.

Pictet ES stock-purchase advices share the trade-advice skeleton
used by ``SUSCRIPCION`` etc. The 2022 fixture exercises the non-FX,
zero-fee path with an all-caps ``COMPRA`` title. No inline ``open``
directive is emitted — account opens are centralised in
``portfolio.beancount``.
"""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.pictet import PictetCompraTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_compra_2022_classifier_routes_to_compra() -> None:
    """Regression guard for the rule re-balance.

    Before the rule update, the 2022 zero-fee fixture scored higher under
    SUSCRIPCION (0.91) than COMPRA (0.84) because COMPRA's distinguishing
    patterns were ``Corretaje`` / ``Tasa bursátil`` — fee lines that
    are absent on zero-fee documents. Lock the classification in so a
    future rule edit doesn't silently regress.
    """

    classification = LayeredClassifier().classify(_load("compra.2022.txt"))
    assert classification.document_type is DocumentType.COMPRA
    assert classification.confidence > 0.90, (
        f"Expected COMPRA confidence above 0.90 on the 2022 fixture; "
        f"got {classification.confidence:.3f}"
    )


def test_compra_2022_renders_to_golden_beancount() -> None:
    txs = PictetCompraTemplate().extract(_load("compra.2022.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the 2022 fixture"

    classification = Classification(
        document_type=DocumentType.COMPRA,
        confidence=0.95,
        source="rules",
        template_id="pictet.compra.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "compra.2022.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Compra entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
