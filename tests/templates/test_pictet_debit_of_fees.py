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
    assert tx.currency == "GBP"
    # Negative — fees debit the account.
    assert tx.amount == Decimal("-3817.66")
    # No security context on a fee advice.
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration carries the Comment line so the period is human-readable
    # in the beancount output.
    assert "Flat fees 1st quarter 2026" in tx.narration
    assert tx.account_number == "P-999999.999"
