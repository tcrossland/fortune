from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetPaymentTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_payment_template_is_registered() -> None:
    assert "pictet.payment.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.payment.v1"].template_id == "pictet.payment.v1"


def test_payment_extracts_single_transaction() -> None:
    template = PictetPaymentTemplate()
    txs = template.extract(_load("payment.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 1, 9)
    assert tx.settlement_date == date(2026, 1, 9)
    assert tx.currency == "GBP"
    # Net amount is the all-in cash impact: gross (-12'000) + fees (-43.40).
    assert tx.amount == Decimal("-12043.40")
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration carries beneficiary and communication so the audit trail
    # stays self-describing without needing the original PDF.
    assert "FIRST MIDDLE LASTNAMES" in tx.narration
    assert "Liquidity" in tx.narration
    assert tx.account_number == "P-999999.999"


def test_payment_template_rejects_incoming_payment() -> None:
    """An incoming-payment advice should yield nothing — the
    ``Beneficiary`` guard is what protects against classifier mis-routes
    between the two payment-transactions advices."""

    template = PictetPaymentTemplate()
    txs = template.extract(_load("incoming_payment.txt"))
    assert txs == []
