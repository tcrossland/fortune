from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetDebitOfFeesTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_debit_of_fees_template_is_registered() -> None:
    assert "pictet.debit_of_fees.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.debit_of_fees.v1"]
    assert template.template_id == "pictet.debit_of_fees.v1"


def test_debit_of_fees_extracts_single_transaction() -> None:
    template = PictetDebitOfFeesTemplate()
    txs = template.extract(_load("debit_of_fees.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 3, 23)
    assert tx.settlement_date == date(2026, 3, 31)
    assert tx.booking_date == date(2026, 3, 23)
    assert tx.currency == "GBP"
    # Negative — fees debit the account.
    assert tx.amount == Decimal("-3817.66")
    # No security context on a fee advice.
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration uses the ``Period <range>`` form in Pictet's printed
    # dd.mm.yyyy date format — same convention as the ES fee advice.
    assert tx.narration == "Period 01.01.2026 - 31.03.2026"
    assert tx.title == "Debit of fees"
    assert tx.transaction_number == "1177002942"
    assert tx.account_number == "P-999999.999"
    # The fee breakdown is two items — one with a multi-line label
    # (``Administration flat fee`` wraps to ``(subject to VAT)`` then
    # the amount), one single-line. The breakdown helper joins the
    # multi-line label parts with single spaces.
    assert len(tx.fee_breakdown) == 2
    assert tx.fee_breakdown[0].description == "Administration flat fee (subject to VAT)"
    assert tx.fee_breakdown[0].amount == Decimal("-3427.14")
    assert tx.fee_breakdown[0].currency == "GBP"
    assert tx.fee_breakdown[1].description == "Account maintenance fees"
    assert tx.fee_breakdown[1].amount == Decimal("-390.52")
