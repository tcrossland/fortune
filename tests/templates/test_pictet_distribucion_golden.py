"""Golden-file test for the Spanish-locale distribution / dividend render.

Pins the same bank-prefixed two-leg shape that ``DIVIDEND_NOTICE``
uses, with the issuer's Spanish narration / title preserved:

  - Booking-date entry date.
  - Two-string narration: ES title (``"Distribución"``) plus
    ``"Dividendo - <fund>"`` subject line.
  - ``Income:<prefix>:<portfolio>:<ISIN>:Dividend`` posting
    signed-negative (beancount income-account convention).
  - ``Assets:<prefix>:<portfolio>:<currency>`` cash leg
    signed-positive (cash in).
  - Trailing ``no:`` reference comment.

Routes through the same dividend builder as ``DIVIDEND_NOTICE`` —
the income/cash leg shape is identical across locales; only the
title and narration carry the ES vocabulary.
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
from banking_pipeline.templates.pictet import PictetDistribucionTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_distribucion_renders_to_golden_beancount() -> None:
    txs = PictetDistribucionTemplate().extract(_load("distribucion.txt"))
    assert len(txs) == 1, (
        "Expected exactly one transaction from the distribucion fixture"
    )

    classification = Classification(
        document_type=DocumentType.DISTRIBUCION,
        confidence=0.95,
        source="rules",
        template_id="pictet.distribucion.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "distribucion.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Distribución entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
