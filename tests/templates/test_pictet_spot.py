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


def test_spot_extracts_two_legs() -> None:
    template = PictetSpotTemplate()
    txs = template.extract(_load("spot.txt"))

    # Spot trade = two cash legs (sold currency out, bought currency in).
    assert len(txs) == 2
    sold, bought = txs

    assert sold.trade_date == date(2026, 3, 16)
    assert sold.settlement_date == date(2026, 3, 18)
    assert sold.currency == "USD"
    # Sold leg: cash leaves the USD account.
    assert sold.amount == Decimal("-69920.99")
    assert sold.account_number == "P-999999.999"

    # Bought leg shares trade/settlement dates with sold (FX spot is
    # settled T+2 on both sides).
    assert bought.trade_date == date(2026, 3, 16)
    assert bought.settlement_date == date(2026, 3, 18)
    assert bought.currency == "EUR"
    assert bought.amount == Decimal("60711.38")
    assert bought.account_number == "P-999999.999"

    # Shared narration carries the headline so the two legs are
    # rejoinable as a single FX event by source_path or narration.
    assert sold.narration == bought.narration
    assert "Sell USD" in sold.narration
    assert "Buy EUR" in sold.narration
    assert "1.151695" in sold.narration


def test_spot_template_rejects_non_spot_doc() -> None:
    """An FX-forward advice should yield nothing — the title guard is
    what protects against routing FX forwards through the spot template
    (both sit under the FOREIGN EXCHANGE banner)."""

    template = PictetSpotTemplate()
    txs = template.extract(_load("fx_forward.txt"))
    assert txs == []
