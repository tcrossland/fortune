"""Unit tests for ``pictet.buy_bonds.v1``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetBuyBondsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_buy_bonds_template_is_registered() -> None:
    assert "pictet.buy_bonds.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.buy_bonds.v1"]
    assert template.template_id == "pictet.buy_bonds.v1"


def test_buy_bonds_extracts_single_transaction() -> None:
    template = PictetBuyBondsTemplate()
    txs = template.extract(_load("buy_bonds.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.title == "Buy bonds"
    assert tx.trade_date == date(2023, 11, 24)
    assert tx.settlement_date == date(2023, 11, 28)
    assert tx.booking_date == date(2023, 11, 24)
    assert tx.currency == "EUR"
    # Net amount (negative on buy) = -(gross + accrued interest + costs)
    # = -(87760.80 + 1809.12 + 408.28). Pictet prints the components
    # negative inside CASH EFFECT; we preserve the printed sign.
    assert tx.amount == Decimal("-89978.20")
    assert tx.isin == "DE000BU3Z005"
    # ``Executed nominal`` — face value, positive on buy.
    assert tx.quantity == Decimal("90000.00")
    # ``Execution price 97.512%`` → 0.97512 EUR per face unit so the
    # writer's cost-basis ``{0.97512 EUR}`` records the per-unit
    # acquisition cost cleanly for capital-gains tracking.
    assert tx.price == Decimal("0.97512")
    assert tx.security_currency == "EUR"
    # Brokerage from CASH EFFECT's ``Costs`` rolled-up line.
    assert tx.fees == Decimal("-408.28")
    assert tx.fees_currency == "EUR"
    # Accrued interest paid by buyer — Pictet prints negative
    # (cash-out from the buyer's perspective). Preserved as-is; the
    # writer flips the sign when posting to the income account.
    assert tx.accrued_interest == Decimal("-1809.12")
    assert "GERMANY 23/33" in tx.narration
    assert tx.account_number == "K-123456.001"
    assert tx.transaction_number == "920228676"


def test_buy_bonds_rejects_sell_operation() -> None:
    """The template only extracts when ``Operation type`` is
    ``Purchase``; feeding a synthetic ``Sell`` variant should yield
    nothing — the classifier-rule discriminator from ``SELL_BONDS`` is
    reinforced at the template layer."""

    template = PictetBuyBondsTemplate()
    buy_text = (FIXTURES / "buy_bonds.txt").read_text(encoding="utf-8")
    sell_text = buy_text.replace("Operation type Purchase", "Operation type Sell")
    doc = RawDocument(
        path=FIXTURES / "buy_bonds.txt", text=sell_text, page_count=1
    )

    txs = template.extract(doc)
    assert txs == []
