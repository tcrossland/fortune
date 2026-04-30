"""Golden-file test for the direct-equity ``Buy Shares`` render.

First golden to exercise a non-FX security buy *with* a non-zero fee
leg. Pins the expanded fees-leg condition in the writer (formerly
gated on ``tx.is_fx``, now emits whenever ``tx.fees != 0``) so a
future revert to the old condition surfaces as a test failure rather
than a silently unbalanced entry.

Layout: asset leg with cost basis at the execution price (commission
excluded — recorded separately), ``Expenses:<prefix>:Fees:<ccy>`` leg
for the commission, cash leg with the all-in net (gross + fees),
trailing ``no:`` reference comment. No inline ``open`` directive —
account opens are centralised in ``portfolio.beancount``.
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
from banking_pipeline.templates.pictet import PictetBuySharesTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_buy_shares_renders_to_golden_beancount() -> None:
    txs = PictetBuySharesTemplate().extract(_load("buy_shares.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the buy_shares fixture"

    classification = Classification(
        document_type=DocumentType.BUY_SHARES,
        confidence=0.95,
        source="rules",
        template_id="pictet.buy_shares.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "buy_shares.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Buy Shares entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
