from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetFxForwardTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_fx_forward_template_is_registered() -> None:
    assert "pictet.fx_forward.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.fx_forward.v1"].template_id == "pictet.fx_forward.v1"


def test_fx_forward_extracts_two_zero_legs() -> None:
    template = PictetFxForwardTemplate()
    txs = template.extract(_load("fx_forward.txt"))

    # Two legs even though both are zero — the contract opens and we
    # record the event for audit completeness.
    assert len(txs) == 2
    usd_leg, gbp_leg = txs

    assert usd_leg.trade_date == date(2026, 2, 4)
    assert usd_leg.settlement_date == date(2026, 2, 5)
    assert usd_leg.currency == "USD"
    assert usd_leg.amount == Decimal("0.00")

    assert gbp_leg.currency == "GBP"
    assert gbp_leg.amount == Decimal("0.00")

    # Narration marks this as the open leg of the forward — distinguishes
    # from settle_fx_forward in downstream beancount rendering.
    assert "FX forward" in usd_leg.narration
    assert "open" in usd_leg.narration
    assert "Buy USD" in usd_leg.narration
    assert usd_leg.narration == gbp_leg.narration


def test_fx_forward_template_rejects_settle_advice() -> None:
    """A settle FX forward advice has the superstring title
    ``Settle FX forward``; the open template must explicitly reject it
    to avoid losing the actual cash legs to a zero-amount extraction."""

    template = PictetFxForwardTemplate()
    txs = template.extract(_load("settle_fx_forward.txt"))
    assert txs == []
