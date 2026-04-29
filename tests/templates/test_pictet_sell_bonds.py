"""Unit tests for ``pictet.sell_bonds.v1`` — bond-sale advice extraction."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSellBondsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_sell_bonds_template_is_registered() -> None:
    assert "pictet.sell_bonds.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.sell_bonds.v1"]
    assert template.template_id == "pictet.sell_bonds.v1"


def test_sell_bonds_extracts_single_transaction() -> None:
    template = PictetSellBondsTemplate()
    txs = template.extract(_load("sell_bonds.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.title == "Sell bonds"
    assert tx.trade_date == date(2023, 12, 20)
    assert tx.settlement_date == date(2023, 12, 22)
    assert tx.booking_date == date(2023, 12, 20)
    assert tx.currency == "EUR"
    # Net amount = gross + accrued interest - costs (92611.80 + 1945.23 - 428.23).
    assert tx.amount == Decimal("94128.80")
    # Real ISIN — DE checksum-valid.
    assert tx.isin == "DE000BU3Z005"
    # ``Executed nominal`` — face value (negative on sell, mirroring
    # Pictet's printed sign).
    assert tx.quantity == Decimal("-90000.00")
    # ``Execution price 102.902%`` → 1.02902 EUR per face unit so the
    # writer's ``@ <price> EUR`` annotation produces a beancount price
    # that yields ``nominal × price = 92611.80`` (the principal).
    assert tx.price == Decimal("1.02902")
    assert tx.security_currency == "EUR"
    # Sale commission from the CASH EFFECT block's ``Costs`` line.
    assert tx.fees == Decimal("-428.23")
    assert tx.fees_currency == "EUR"
    # Bond-specific accrued interest line.
    assert tx.accrued_interest == Decimal("1945.23")
    assert "GERMANY 23/33" in tx.narration
    assert tx.account_number == "K-123456.001"
    assert tx.transaction_number == "928806826"


def test_sell_bonds_rejects_buy_operation() -> None:
    """Defence-in-depth against classifier mis-route. The template only
    extracts when ``Operation type`` is ``Sell``; feeding a synthetic
    ``Buy`` variant should yield nothing."""

    template = PictetSellBondsTemplate()
    sell_text = (FIXTURES / "sell_bonds.txt").read_text(encoding="utf-8")
    buy_text = sell_text.replace("Operation type Sell", "Operation type Buy")
    doc = RawDocument(path=FIXTURES / "sell_bonds.txt", text=buy_text, page_count=1)

    txs = template.extract(doc)
    assert txs == []
