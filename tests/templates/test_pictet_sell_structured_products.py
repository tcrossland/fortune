"""Unit tests for ``pictet.sell_structured_products.v1``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSellStructuredProductsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_sell_structured_products_template_is_registered() -> None:
    assert "pictet.sell_structured_products.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.sell_structured_products.v1"]
    assert template.template_id == "pictet.sell_structured_products.v1"


def test_sell_structured_products_extracts_single_transaction() -> None:
    template = PictetSellStructuredProductsTemplate()
    txs = template.extract(_load("sell_structured_products.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.title == "Sell structured products"
    assert tx.trade_date == date(2024, 4, 18)
    assert tx.settlement_date == date(2024, 4, 22)
    assert tx.booking_date == date(2024, 4, 18)
    assert tx.currency == "EUR"
    # Net amount = gross (no costs on this fixture).
    assert tx.amount == Decimal("17783.55")
    # Real Swiss ISIN — not Pictet's ZZ-prefixed internal ref.
    assert tx.isin == "CH1146387191"
    # Quantity printed negative on the sell side (units leaving).
    assert tx.quantity == Decimal("-405")
    # Price quoted in the trade currency, not as a percentage (the
    # load-bearing distinction from SELL_BONDS).
    assert tx.price == Decimal("43.91")
    assert tx.security_currency == "EUR"
    # No fees on this fixture (Costs EUR 0.00).
    assert tx.fees == Decimal("0.00")
    assert tx.fees_currency == "EUR"
    assert "PWM LG VOL BALANC" in tx.narration
    assert tx.account_number == "K-123456.001"
    assert tx.transaction_number == "970189146"


def test_sell_structured_products_rejects_buy_operation() -> None:
    """The template only extracts when ``Operation type`` is ``Sell``;
    feeding a synthetic ``Buy`` variant should yield nothing — the
    classifier-rule discriminator is reinforced at the template layer."""

    template = PictetSellStructuredProductsTemplate()
    sell_text = (FIXTURES / "sell_structured_products.txt").read_text(
        encoding="utf-8"
    )
    buy_text = sell_text.replace("Operation type Sell", "Operation type Buy")
    doc = RawDocument(
        path=FIXTURES / "sell_structured_products.txt",
        text=buy_text,
        page_count=1,
    )

    txs = template.extract(doc)
    assert txs == []
