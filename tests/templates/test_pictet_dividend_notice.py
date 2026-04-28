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
    # Trade date == Ex date; settlement_date == Payment date; booking_date
    # == when the cash actually moved (used as the entry date by the
    # writer). Keeping the mapping tight with the other Pictet advices.
    assert tx.trade_date == date(2026, 2, 2)
    assert tx.settlement_date == date(2026, 2, 20)
    assert tx.booking_date == date(2026, 2, 24)
    assert tx.currency == "GBP"
    assert tx.amount == Decimal("1242.50")
    # Quantity held = the position that generated the dividend, not a
    # transferred quantity. 994.000 × 1.25 = 1242.50 (matches Net amount).
    assert tx.quantity == Decimal("994.000")
    assert tx.price == Decimal("1.25")
    assert tx.isin == "LU2096759431"
    # Narration uses the ``Dividend - <fund>`` security-event subject
    # line, mirroring ``reembolso_final``'s ``Reembolso - <fund>``.
    assert tx.narration == "Dividend - JPMF-INCOME FD C (DIV) GBP H-INC.-"
    assert tx.title == "Dividend"
    assert tx.transaction_number == "1168218479"
    assert tx.account_number == "P-999999.999"
