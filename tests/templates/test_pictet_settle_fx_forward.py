from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSettleFxForwardTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_settle_fx_forward_template_is_registered() -> None:
    assert "pictet.settle_fx_forward.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.settle_fx_forward.v1"]
    assert template.template_id == "pictet.settle_fx_forward.v1"


def test_settle_fx_forward_extracts_two_legs() -> None:
    template = PictetSettleFxForwardTemplate()
    txs = template.extract(_load("settle_fx_forward.txt"))

    assert len(txs) == 2
    usd_leg, gbp_leg = txs

    assert usd_leg.trade_date == date(2026, 2, 4)
    assert usd_leg.settlement_date == date(2026, 2, 5)
    assert usd_leg.currency == "USD"
    # Bought leg: cash lands in USD account.
    assert usd_leg.amount == Decimal("24660.48")

    # Sold leg: Net = Gross (-17'992.08) + Costs (-53.98 forward spread)
    # = -18'046.06. We use the all-in Net so beancount postings reflect
    # the actual cash impact, including the spread.
    assert gbp_leg.currency == "GBP"
    assert gbp_leg.amount == Decimal("-18046.06")

    assert "FX forward" in usd_leg.narration
    assert "settle" in usd_leg.narration
    assert usd_leg.narration == gbp_leg.narration


def test_settle_fx_forward_template_rejects_open_advice() -> None:
    """A plain FX-forward (open) advice should yield nothing — the
    settle template requires the ``Settle FX forward`` title and bails
    when only ``FX forward`` is present."""

    template = PictetSettleFxForwardTemplate()
    txs = template.extract(_load("fx_forward.txt"))
    assert txs == []
