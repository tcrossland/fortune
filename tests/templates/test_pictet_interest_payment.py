from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetInterestPaymentTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_interest_payment_template_is_registered() -> None:
    assert "pictet.interest_payment.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.interest_payment.v1"]
    assert template.template_id == "pictet.interest_payment.v1"


def test_interest_payment_extracts_single_transaction() -> None:
    template = PictetInterestPaymentTemplate()
    txs = template.extract(_load("interest_payment.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 3, 31)
    assert tx.settlement_date == date(2026, 3, 31)
    assert tx.booking_date == date(2026, 3, 31)
    assert tx.currency == "GBP"
    # Debit-balance interest charge — negative amount. The writer's
    # ``_render_interest`` path keys the counter-leg account family
    # (Expenses vs Income) on this sign.
    assert tx.amount == Decimal("-16858.14")
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration uses the ``Period <range>`` form in Pictet's printed
    # dd.mm.yyyy date format — same convention as the fee-advice path.
    assert tx.narration == "Period 31.12.2025 - 31.03.2026"
    assert tx.title == "Interest payment"
    assert tx.transaction_number == "1180262700"
    assert tx.account_number == "P-999999.999"
