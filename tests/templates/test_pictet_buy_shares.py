from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetBuySharesTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_buy_shares_template_is_registered() -> None:
    assert "pictet.buy_shares.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.buy_shares.v1"]
    assert template.template_id == "pictet.buy_shares.v1"


def test_buy_shares_classifier_routes_to_buy_shares() -> None:
    """Lock in classifier routing — the standalone ``Buy Shares`` title
    plus the ``Asset type Equities`` discriminator together separate
    this from BUY_ETF / BUY_STRUCTURED_PRODUCTS / SUBSCRIPTION_NOTICE.
    Without the dedicated rule the fixture ties between BUY_ETF and
    SUBSCRIPTION_NOTICE at ~0.84 and gets dropped because both
    templates' title gates miss the ``Buy Shares`` banner."""

    classification = LayeredClassifier().classify(_load("buy_shares.txt"))
    assert classification.document_type is DocumentType.BUY_SHARES
    assert classification.confidence > 0.90, (
        f"Expected BUY_SHARES confidence above 0.90 on the fixture; "
        f"got {classification.confidence:.3f}"
    )


def test_buy_shares_extracts_single_transaction() -> None:
    template = PictetBuySharesTemplate()
    txs = template.extract(_load("buy_shares.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2025, 4, 3)
    assert tx.settlement_date == date(2025, 4, 7)
    assert tx.booking_date == date(2025, 4, 3)
    # Non-FX equity buy: security and cash both DKK.
    assert tx.currency == "DKK"
    assert tx.security_currency == "DKK"
    assert tx.is_fx is False
    # Net amount: gross (-92'034.95) + fees (-1'201.37) = -93'236.32.
    assert tx.amount == Decimal("-93236.32")
    assert tx.quantity == Decimal("200")
    assert tx.price == Decimal("460.17475")
    assert tx.isin == "DK0062498333"
    # Commission/Fee — tx.fees populated from the CASH EFFECT block's
    # ``Costs DKK -1'201.37`` line. The writer emits a dedicated
    # ``Expenses:<prefix>:Fees:<ccy>`` leg for the entry to balance
    # (asset = gross only, cash = gross + fees).
    assert tx.fees == Decimal("-1201.37")
    assert tx.fees_currency == "DKK"
    # Headline: "Buy 200 NOVO NORDISK 'B' at DKK 460.17475".
    assert tx.narration == "Buy 200 NOVO NORDISK 'B' at DKK 460.17475"
    assert tx.title == "Buy Shares"
    assert tx.transaction_number == "1068597538"
    assert tx.account_number == "P-123456.002"
