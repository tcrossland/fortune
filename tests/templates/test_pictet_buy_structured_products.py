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
    # The fixture's anonymised ``ISIN/Internal ref.`` value carries the
    # space-before-final-char artifact Pictet's PDF extractor produces
    # on structured-product internal refs (``ZZ00ABB5K5 0``). The
    # parser strips the space and returns the contiguous 11-char form
    # so the writer can use it as a stable beancount commodity. The
    # value won't pass ISIN checksum validation (Pictet codes aren't
    # real ISINs) and ``resolve_isin`` falls back to the raw form.
    assert tx.isin == "ZZ00ABB5K50"
    assert "PWM LG VOL BALANC" in tx.narration
    assert tx.account_number == "P-999999.999"
