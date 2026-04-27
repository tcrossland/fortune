from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetDebitoDeGastosTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_debito_de_gastos_template_is_registered() -> None:
    assert "pictet.debito_de_gastos.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.debito_de_gastos.v1"]
    assert template.template_id == "pictet.debito_de_gastos.v1"


def test_debito_de_gastos_extracts_single_transaction() -> None:
    template = PictetDebitoDeGastosTemplate()
    txs = template.extract(_load("debito_de_gastos.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 3, 20)
    assert tx.settlement_date == date(2023, 3, 31)
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-3488.36")
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # ``Comentario`` line carries human-readable context.
    assert "Honorarios de administración 1° trimestre 2023" in tx.narration
    assert tx.account_number == "P-999999.999"
