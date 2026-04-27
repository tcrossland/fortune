from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetCompraTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_compra_template_is_registered() -> None:
    assert "pictet.compra.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.compra.v1"].template_id == "pictet.compra.v1"


def test_compra_extracts_single_transaction() -> None:
    template = PictetCompraTemplate()
    txs = template.extract(_load("compra.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 1, 23)
    assert tx.settlement_date == date(2023, 1, 25)
    # FX inside the cash-effect block: trade in USD, cash impact in EUR.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-35074.08")
    assert tx.quantity == Decimal("3401")
    assert tx.price == Decimal("11.098")
    assert tx.isin == "LU1852211215"
    assert "Compra" in tx.narration
    assert "UBS(LUX)" in tx.narration
    assert tx.account_number == "P-999999.999"


def test_compra_template_rejects_suscripcion_doc() -> None:
    """``suscripcion`` carries a different title; the standalone-title
    check is the discriminator."""

    template = PictetCompraTemplate()
    txs = template.extract(_load("suscripcion.txt"))
    assert txs == []
