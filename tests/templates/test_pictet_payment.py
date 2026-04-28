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
    assert tx.booking_date == date(2026, 1, 9)
    assert tx.currency == "GBP"
    # Net amount is the all-in cash impact: gross (-12'000) + fees (-43.40).
    # Negative sign tells the writer's third-party-payment path to use
    # ``Expenses:<prefix>:Other`` for the elastic counter-leg.
    assert tx.amount == Decimal("-12043.40")
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration combines beneficiary with the Communication wire memo.
    assert tx.narration == "FIRST MIDDLE LASTNAMES - Liquidity"
    assert tx.title == "Payment"
    assert tx.transaction_number == "1154839947"
    assert tx.account_number == "P-999999.999"
    # Self-to-self detection: ``Beneficiary`` matches the account
    # holder (FIRST MIDDLE LASTNAMES is also the Client name on the
    # advice header). The bank field maps to ``Revolut`` via
    # ``settings.beneficiary_bank_map``. The writer uses
    # ``gross_amount`` / ``counter_account`` to emit the three-leg
    # form (destination bank → Pictet → wire fee), distinct from the
    # two-leg-elastic shape used for genuine third-party payments.
    assert tx.gross_amount == Decimal("12000.00")
    assert tx.counter_account == "Revolut"
    assert tx.fees == Decimal("-43.40")
    assert tx.fees_currency == "GBP"


def test_payment_template_rejects_incoming_payment() -> None:
    """An incoming-payment advice should yield nothing — the
    ``Beneficiary`` guard is what protects against classifier mis-routes
    between the two payment-transactions advices."""

    template = PictetPaymentTemplate()
    txs = template.extract(_load("incoming_payment.txt"))
    assert txs == []
