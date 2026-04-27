from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetReembolsoTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_reembolso_template_is_registered() -> None:
    assert "pictet.reembolso.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.reembolso.v1"].template_id == "pictet.reembolso.v1"


def test_reembolso_extracts_single_transaction() -> None:
    template = PictetReembolsoTemplate()
    txs = template.extract(_load("reembolso.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 2, 24)
    assert tx.settlement_date == date(2023, 3, 1)
    # Trade currency = cash currency on this fixture (no FX inside the
    # cash-effect block since proceeds land in the USD account).
    assert tx.currency == "USD"
    assert tx.amount == Decimal("278754.45")
    assert tx.quantity == Decimal("-6357.000")
    assert tx.price == Decimal("43.85")
    assert tx.isin == "LU0128316170"
    assert "Venta" in tx.narration
    assert tx.account_number == "P-999999.999"


def test_reembolso_template_rejects_suscripcion_doc() -> None:
    template = PictetReembolsoTemplate()
    txs = template.extract(_load("suscripcion.txt"))
    assert txs == []
