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


def test_settle_fx_forward_extracts_single_cross_currency_transaction() -> None:
    """The template now produces ONE Transaction with both legs' info
    plus the forward spread fee, mirroring ``internal_transfer``. The
    writer's ``_render_fx_settlement`` path uses
    ``currency``/``amount`` (the fee-bearing leg),
    ``counter_currency``/``counter_amount`` (the other leg), and
    ``fees``/``fees_currency`` to emit a single beancount entry with
    a fee posting and an ``@@`` annotation linking the two cash
    currencies."""

    template = PictetSettleFxForwardTemplate()
    txs = template.extract(_load("settle_fx_forward.txt"))

    assert len(txs) == 1
    tx = txs[0]

    assert tx.trade_date == date(2026, 2, 4)
    assert tx.settlement_date == date(2026, 2, 5)
    assert tx.booking_date == date(2026, 2, 4)

    # Fee-bearing leg: GBP, signed negative (sold side, cash leaving
    # the GBP account; ``Net amount = Gross + Costs = -17'992.08 +
    # -53.98 = -18'046.06``).
    assert tx.currency == "GBP"
    assert tx.amount == Decimal("-18046.06")

    # Counter leg: USD, signed positive (bought side, cash arriving
    # in the USD account).
    assert tx.counter_currency == "USD"
    assert tx.counter_amount == Decimal("24660.48")

    # Forward spread, signed as printed (Pictet writes negative; the
    # writer flips sign for the expense posting).
    assert tx.fees == Decimal("-53.98")
    assert tx.fees_currency == "GBP"

    assert tx.title == "Settle FX forward"
    assert tx.narration == "Buy USD 24'660.48 - Sell GBP at 1.3665295"
    assert tx.transaction_number == "1162235443"
    # Account number falls back to the portfolio header (anonymised
    # IBANs in the fixtures don't validate).
    assert tx.account_number == "P-999999.999"


def test_settle_fx_forward_extracts_2025_fixture() -> None:
    """Mirror of the 2026 fixture with the trade direction reversed:
    Operation type Sell, fee on the buy/cash-in leg (EUR), counter
    leg on the sell/cash-out side (GBP). Pins the fee-leg-detection
    logic — we identify the fee-bearing leg by matching the
    ``Forward spread <CCY>`` line, not by sign or operation type."""

    template = PictetSettleFxForwardTemplate()
    txs = template.extract(_load("settle_fx_forward.2025.txt"))

    assert len(txs) == 1
    tx = txs[0]

    assert tx.trade_date == date(2025, 12, 19)
    assert tx.settlement_date == date(2025, 12, 22)

    # Fee on EUR (the bought side this time): Net is signed positive
    # because cash arrives in the EUR account, and Pictet writes the
    # spread as the difference between gross + costs = 5'708.33 +
    # -17.12 = 5'691.21.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("5691.21")
    assert tx.fees == Decimal("-17.12")
    assert tx.fees_currency == "EUR"

    # Counter leg: GBP cash-out.
    assert tx.counter_currency == "GBP"
    assert tx.counter_amount == Decimal("-5000.00")

    assert tx.narration == "Sell GBP 5'000.00 - Buy EUR at 0.8785475"


def test_settle_fx_forward_template_rejects_open_advice() -> None:
    """A plain FX-forward (open) advice should yield nothing — the
    settle template requires the ``Settle FX forward`` title and bails
    when only ``FX forward`` is present."""

    template = PictetSettleFxForwardTemplate()
    txs = template.extract(_load("fx_forward.txt"))
    assert txs == []
