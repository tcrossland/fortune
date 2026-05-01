"""Golden-file test for the 2025 ``Sell structured products`` fixture.

Distinct from ``test_pictet_sell_structured_products_golden`` (the
no-breakdown 2024 fixture) in that the 2025 advice carries a
multi-item cost block:

    Costs
    Commission/Fee USD -200.79
    Transaction taxes USD -3.62
    Total USD -204.41

The writer's dispatch in ``_render_security_trade`` routes
multi-item-breakdown sells to ``_render_security_sell_with_breakdown``
(asset-first ordering, one ``Expenses:<prefix>:<portfolio>:Fees:<ccy>``
posting per breakdown item with inline ``; <description>`` comment,
then the cash leg, then the elastic
``Income:<prefix>:<portfolio>:<ISIN>:Realized`` leg).

This pin is the second fixture exercising the breakdown renderer
after ``venta.beancount`` (Spanish-locale stock sell). The two
share the same posting layout and the same ``:Realized`` suffix on
the income leg.
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
from banking_pipeline.templates.pictet import (
    PictetSellStructuredProductsTemplate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_sell_structured_products_2025_renders_to_golden_beancount() -> None:
    txs = PictetSellStructuredProductsTemplate().extract(
        _load("sell_structured_products.2025.txt")
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
        FIXTURES / "sell_structured_products.2025.beancount"
    ).read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Sell structured products 2025 entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
