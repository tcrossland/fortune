from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSpotTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_spot_template_is_registered() -> None:
    assert "pictet.spot.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.spot.v1"].template_id == "pictet.spot.v1"


def test_spot_extracts_single_two_leg_transaction() -> None:
    template = PictetSpotTemplate()
    txs = template.extract(_load("spot.txt"))

    # The two CASH EFFECT blocks collapse into one Transaction —
    # source on currency/amount (signed negative, cash out), dest on
    # counter_currency/counter_amount (signed positive, cash in).
    # Mirrors INTERNAL_TRANSFER's shape so the writer's
    # ``_render_internal_transfer`` builder can render both with the
    # same ``@@ <abs_source> <src_ccy>`` annotation form.
    assert len(txs) == 1
    tx = txs[0]

    assert tx.title == "Spot"
    assert tx.trade_date == date(2026, 3, 16)
    assert tx.settlement_date == date(2026, 3, 18)
    assert tx.booking_date == date(2026, 3, 16)
    # Source (debit) leg.
    assert tx.currency == "USD"
    assert tx.amount == Decimal("-69920.99")
    # Destination (credit) leg.
    assert tx.counter_currency == "EUR"
    assert tx.counter_amount == Decimal("60711.38")
    # Both legs sit in the same Pictet portfolio.
    assert tx.account_number == "P-999999.999"
    # Headline preserved as narration.
    assert "Sell USD" in tx.narration
    assert "Buy EUR" in tx.narration
    assert "1.151695" in tx.narration
    # Exchange rate parsed off the in-block ``Execution rate`` line.
    assert tx.exchange_rate == Decimal("1.151695")
    assert tx.transaction_number == "1174874179"


def test_spot_template_rejects_non_spot_doc() -> None:
    """An FX-forward advice should yield nothing — the title guard is
    what protects against routing FX forwards through the spot template
    (both sit under the FOREIGN EXCHANGE banner)."""

    template = PictetSpotTemplate()
    txs = template.extract(_load("fx_forward.txt"))
    assert txs == []
