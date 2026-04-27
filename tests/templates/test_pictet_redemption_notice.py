from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetRedemptionNoticeTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_redemption_notice_template_is_registered() -> None:
    assert "pictet.redemption_notice.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.redemption_notice.v1"]
    assert template.template_id == "pictet.redemption_notice.v1"


def test_redemption_notice_extracts_single_transaction() -> None:
    template = PictetRedemptionNoticeTemplate()
    txs = template.extract(_load("redemption_notice.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2025, 10, 17)
    assert tx.settlement_date == date(2025, 10, 21)
    # Sale = cash in, so the amount carries the printed positive sign.
    assert tx.currency == "USD"
    assert tx.amount == Decimal("119613.69")
    # Quantity is negative on a redemption (units leaving the portfolio) —
    # we preserve Pictet's sign rather than collapsing to absolute value.
    assert tx.quantity == Decimal("-261.00000")
    assert tx.price == Decimal("458.29")
    assert tx.isin == "LU0503632100"
    assert "GLOB ENVIR OPP-I USD" in tx.narration
    assert tx.account_number == "P-999999.999"


def test_redemption_template_rejects_non_sale_doc() -> None:
    """A subscription advice fed to the redemption template should yield
    nothing — the ``expected_operations`` guard is what protects downstream
    code from classifier mis-routes."""

    template = PictetRedemptionNoticeTemplate()
    txs = template.extract(_load("subscription_notice.txt"))
    assert txs == []
