"""Unit tests for ``pictet.sell_etf.v1``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSellEtfTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_sell_etf_template_is_registered() -> None:
    assert "pictet.sell_etf.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.sell_etf.v1"]
    assert template.template_id == "pictet.sell_etf.v1"


def test_sell_etf_extracts_single_transaction() -> None:
    template = PictetSellEtfTemplate()
    txs = template.extract(_load("sell_etf.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.title == "Sell Exchange Traded Fund"
    assert tx.trade_date == date(2024, 11, 7)
    assert tx.settlement_date == date(2024, 11, 11)
    assert tx.booking_date == date(2024, 11, 7)
    assert tx.currency == "EUR"
    # Net amount = gross (244436.14) - costs (1099.96).
    assert tx.amount == Decimal("243336.18")
    # Real Luxembourg ISIN (Multi Units SICAV / Amundi ETF).
    assert tx.isin == "LU1287023185"
    # Quantity printed negative on the sell side (units leaving).
    assert tx.quantity == Decimal("-1488")
    # Price quoted in trade currency (EUR), not as a percentage —
    # the load-bearing distinction from SELL_BONDS.
    assert tx.price == Decimal("164.2716")
    assert tx.security_currency == "EUR"
    # Sale commission from the CASH EFFECT block's ``Costs`` line.
    assert tx.fees == Decimal("-1099.96")
    assert tx.fees_currency == "EUR"
    assert "Multi Units Luxembourg SICAV" in tx.narration
    assert tx.account_number == "K-123456.001"
    assert tx.transaction_number == "1027385859"


def test_sell_etf_rejects_buy_operation() -> None:
    """The template only extracts when ``Operation type`` is ``Sell``;
    feeding a synthetic ``Buy`` variant should yield nothing — the
    classifier-rule discriminator is reinforced at the template layer."""

    template = PictetSellEtfTemplate()
    sell_text = (FIXTURES / "sell_etf.txt").read_text(encoding="utf-8")
    buy_text = sell_text.replace("Operation type Sell", "Operation type Buy")
    doc = RawDocument(
        path=FIXTURES / "sell_etf.txt", text=buy_text, page_count=1
    )

    txs = template.extract(doc)
    assert txs == []
