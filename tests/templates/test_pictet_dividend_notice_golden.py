"""Golden-file test for the dividend / distribution render.

Pins the bank-prefixed two-leg dividend shape:

  - Booking-date entry date.
  - Two-string narration: canonical title (``"Dividend"``) plus
    ``"Dividend - <fund>"`` subject line.
  - ``Income:<prefix>:<ISIN>:Dividend`` posting signed-negative
    (beancount income-account convention).
  - ``Assets:<prefix>:<currency>`` cash leg signed-positive (cash in).
  - Trailing ``no:`` reference comment.

Replaces the legacy two-leg form
``Income:Dividends:<ISIN>`` + ``Assets:Broker:Cash`` that the prior
Jinja template produced. The new shape composes cleanly with the
bank-prefixed account hierarchy used by the security-trade and
switch paths.
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
from banking_pipeline.templates.pictet import PictetDividendNoticeTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_dividend_notice_renders_to_golden_beancount() -> None:
    txs = PictetDividendNoticeTemplate().extract(_load("dividend_notice.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the dividend fixture"

    classification = Classification(
        document_type=DocumentType.DIVIDEND_NOTICE,
        confidence=0.95,
        source="rules",
        template_id="pictet.dividend_notice.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "dividend_notice.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Dividend entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
