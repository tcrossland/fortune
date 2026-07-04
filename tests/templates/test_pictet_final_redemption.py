from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetFinalRedemptionTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_final_redemption_template_is_registered() -> None:
    assert "pictet.final_redemption.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.final_redemption.v1"]
    assert template.template_id == "pictet.final_redemption.v1"


def test_final_redemption_extracts_single_transaction() -> None:
    template = PictetFinalRedemptionTemplate()
    txs = template.extract(_load("final_redemption.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 3, 30)
    assert tx.settlement_date == date(2026, 3, 30)
    assert tx.currency == "EUR"
    # Net amount = positive (cash proceeds).
    assert tx.amount == Decimal("25799.83")
    # Quantity printed negative on the security-event line (units leaving).
    assert tx.quantity == Decimal("-423")
    assert tx.price == Decimal("60.99250934")
    # Pictet's ISIN/Internal ref. line for structured products has the
    # PDF-extractor space artifact (``ZZ00AB97OD 0``); ``find_isin``
    # strips the space and returns the contiguous 11-char form.
    # ``resolve_isin`` falls back to the raw value because the code
    # isn't a real (checksummed) ISIN.
    assert tx.isin == "ZZ00AB97OD0"
    assert "PWM LG VOL BALANC" in tx.narration
    assert tx.account_number == "P-999999.999"
    assert tx.transaction_number == "1178982635"


def test_final_redemption_rejects_fund_redemption() -> None:
    """A fund redemption uses ``Execution price`` rather than
    ``Redemption price`` — feeding one to this template should yield
    nothing."""

    template = PictetFinalRedemptionTemplate()
    txs = template.extract(_load("redemption_notice.txt"))
    assert txs == []
