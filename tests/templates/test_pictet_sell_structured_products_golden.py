"""Golden-file test for the EN structured-product sale render.

Pins the four-leg shape ``SELL_STRUCTURED_PRODUCTS`` produces by
routing through the existing ``_render_security_trade`` builder via
``_SECURITY_SELL_TYPES`` membership:

  - Cash leg (Net amount, positive — proceeds in).
  - Asset leg (units leaving inventory at the trade price, with the
    empty-cost ``{}`` form so beancount reduces the position at its
    cost basis and ``@ <price>`` records the sale price for
    capital-gains attribution).
  - Elastic ``Income:<prefix>:<isin>:Realized`` leg.
  - Trailing ``no:`` reference comment.

The fees leg is omitted because the fixture's CASH EFFECT block
carries ``Costs EUR 0.00``; the writer's ``tx.fees != 0`` guard
suppresses the leg in that case (same shape used by the zero-fee
``compra.2022`` fixture).
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
from banking_pipeline.templates.pictet import PictetSellStructuredProductsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_sell_structured_products_renders_to_golden_beancount() -> None:
    txs = PictetSellStructuredProductsTemplate().extract(
        _load("sell_structured_products.txt")
    )
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.SELL_STRUCTURED_PRODUCTS,
        confidence=0.95,
        source="rules",
        template_id="pictet.sell_structured_products.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (
        FIXTURES / "sell_structured_products.beancount"
    ).read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Sell structured products entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
