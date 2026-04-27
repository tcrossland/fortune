from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetLimitExtensionTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_limit_extension_template_is_registered() -> None:
    assert "pictet.limit_extension.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.limit_extension.v1"]
    assert template.template_id == "pictet.limit_extension.v1"


def test_limit_extension_extracts_zero_amount_event() -> None:
    template = PictetLimitExtensionTemplate()
    txs = template.extract(_load("limit_extension.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 2, 25)
    assert tx.settlement_date == date(2026, 2, 26)
    assert tx.currency == "GBP"
    # Pictet prints CASH EFFECT Net amount as 0.00 — preserved as-is so
    # downstream rendering can choose to skip the posting or emit a
    # zero-amount transaction.
    assert tx.amount == Decimal("0.00")
    assert tx.isin is None
    # Narration carries the C/a limit descriptor so the event records
    # which limit was extended and over what period.
    assert "C/a limit GBP" in tx.narration
    assert "26.02.2025-26.02.2026" in tx.narration
    assert tx.account_number == "P-999999.999"
