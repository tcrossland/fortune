from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetBuyStructuredProductsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_buy_structured_products_template_is_registered() -> None:
    assert "pictet.buy_structured_products.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.buy_structured_products.v1"]
    assert template.template_id == "pictet.buy_structured_products.v1"


def test_buy_structured_products_extracts_single_transaction() -> None:
    template = PictetBuyStructuredProductsTemplate()
    txs = template.extract(_load("buy_structured_products.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 3, 26)
    assert tx.settlement_date == date(2026, 3, 30)
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-23700.00")
    assert tx.quantity == Decimal("474")
    assert tx.price == Decimal("50.00")
    # The fixture's anonymised ``ISIN/Internal ref.`` value contains a stray
    # space (``ZZ00ABB5K5 0``) which our 12-contiguous-char ISIN regex
    # deliberately rejects — matching arbitrary spaces would create false
    # positives elsewhere in the doc. Real Pictet structured products carry
    # contiguous codes that do match. Not asserting a specific value, but
    # the test pins the current behaviour so a future relaxation is a
    # deliberate decision rather than an accident.
    assert tx.isin is None
    assert "PWM LG VOL BALANC" in tx.narration
    assert tx.account_number == "P-999999.999"
