from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetDividendNoticeTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_dividend_notice_template_is_registered() -> None:
    assert "pictet.dividend_notice.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.dividend_notice.v1"]
    assert template.template_id == "pictet.dividend_notice.v1"


def test_dividend_notice_extracts_single_transaction() -> None:
    template = PictetDividendNoticeTemplate()
    txs = template.extract(_load("dividend_notice.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Trade date == Ex date; settlement_date == Payment date. Keeping the
    # mapping tight with the other Pictet advices.
    assert tx.trade_date == date(2026, 2, 2)
    assert tx.settlement_date == date(2026, 2, 20)
    assert tx.currency == "GBP"
    assert tx.amount == Decimal("1242.50")
    # Quantity held = the position that generated the dividend, not a
    # transferred quantity. 994.000 × 1.25 = 1242.50 (matches Net amount).
    assert tx.quantity == Decimal("994.000")
    assert tx.price == Decimal("1.25")
    assert tx.isin == "LU2096759431"
    assert "JPMF-INCOME FD" in tx.narration
    assert tx.account_number == "P-999999.999"
