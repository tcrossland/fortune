from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetFacturaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_factura_template_is_registered() -> None:
    assert "pictet.factura.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.factura.v1"]
    assert template.template_id == "pictet.factura.v1"


def test_factura_extracts_single_transaction() -> None:
    template = PictetFacturaTemplate()
    txs = template.extract(_load("factura.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 3, 23)
    assert tx.settlement_date == date(2026, 3, 31)
    assert tx.currency == "GBP"
    # The invoice prints ``Total GBP 5'140.70`` as a positive line item;
    # we negate for cash-impact convention. A real factura with a refund
    # (negative invoice) would correspondingly produce a positive amount.
    assert tx.amount == Decimal("-5140.70")
    assert tx.isin is None
    # Narration carries the invoice number and period for audit purposes.
    assert "factura n° 80" in tx.narration
    assert "Honorarios de gestión" in tx.narration
    assert "2026-01-01" in tx.narration
    assert tx.account_number == "P-999999.999"
