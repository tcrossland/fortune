from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetIncomingPaymentTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_incoming_payment_template_is_registered() -> None:
    assert "pictet.incoming_payment.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.incoming_payment.v1"]
    assert template.template_id == "pictet.incoming_payment.v1"


def test_incoming_payment_extracts_single_transaction() -> None:
    template = PictetIncomingPaymentTemplate()
    txs = template.extract(_load("incoming_payment.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 2, 16)
    assert tx.settlement_date == date(2026, 2, 16)
    assert tx.currency == "EUR"
    # Positive: cash incoming.
    assert tx.amount == Decimal("200000.00")
    assert tx.isin is None
    assert "Nilufer Keskin" in tx.narration
    # The Comment block carries free-form context — preserve it in narration.
    assert "UVZ Nr. 1445/2025" in tx.narration
    assert tx.account_number == "P-999999.999"


def test_incoming_payment_template_rejects_outgoing_payment() -> None:
    """An outgoing-payment advice has ``Beneficiary`` but no
    ``Instructing party`` — the incoming template should bail rather than
    parse a sign-flipped transaction."""

    template = PictetIncomingPaymentTemplate()
    txs = template.extract(_load("payment.txt"))
    assert txs == []
