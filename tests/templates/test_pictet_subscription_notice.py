from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSubscriptionNoticeTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_subscription_notice_template_is_registered() -> None:
    assert "pictet.subscription_notice.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.subscription_notice.v1"]
    assert template.template_id == "pictet.subscription_notice.v1"


def test_subscription_notice_extracts_single_transaction() -> None:
    template = PictetSubscriptionNoticeTemplate()
    txs = template.extract(_load("subscription_notice.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Trade vs. value-date split is a stable Pictet field; a regression here
    # almost certainly means the field-line regex broke.
    assert tx.trade_date == date(2025, 10, 20)
    assert tx.settlement_date == date(2025, 10, 22)
    # Cash leg is in EUR for this fixture; signed negative because the client
    # paid out for the subscription.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-119890.47")
    # Quantity / price pair from the Execution block.
    assert tx.quantity == Decimal("469.00")
    assert tx.price == Decimal("255.63")
    # Fixture is anonymised, so the ISIN won't pass stdnum validation; the
    # template should keep the raw value rather than dropping it.
    assert tx.isin == "LU1111643711"
    # Headline narration carries the fund name.
    assert "ELEVA-EUROPEAN SELECTION" in tx.narration
    # Anonymised IBAN won't validate, so we fall back to the Pictet
    # portfolio account ID.
    assert tx.account_number == "P-999999.999"
