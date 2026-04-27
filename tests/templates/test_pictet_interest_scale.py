from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetInterestScaleTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_interest_scale_template_is_registered() -> None:
    assert "pictet.interest_scale.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.interest_scale.v1"]
    assert template.template_id == "pictet.interest_scale.v1"


def test_interest_scale_extracts_total_as_transaction() -> None:
    template = PictetInterestScaleTemplate()
    txs = template.extract(_load("interest_scale.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Period end is the effective date; matches the trade_date Pictet
    # prints on the paired interest_payment advice.
    assert tx.trade_date == date(2026, 3, 31)
    assert tx.settlement_date == date(2026, 3, 31)
    # Currency comes from the BALANCE (USD) column header — this fixture
    # is the USD account, distinct from the GBP interest_payment fixture.
    assert tx.currency == "USD"
    # Total interest from the table's summary row.
    assert tx.amount == Decimal("-3510.65")
    assert tx.isin is None
    assert tx.quantity is None
    assert "2025-12-31" in tx.narration
    assert "2026-03-31" in tx.narration
    assert tx.account_number == "P-999999.999"
