from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSuscripcionTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_suscripcion_template_is_registered() -> None:
    assert "pictet.suscripcion.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.suscripcion.v1"].template_id == "pictet.suscripcion.v1"


def test_suscripcion_extracts_single_transaction() -> None:
    template = PictetSuscripcionTemplate()
    txs = template.extract(_load("suscripcion.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 1, 23)
    assert tx.settlement_date == date(2023, 1, 26)
    # ``Importe neto`` is in EUR even though the trade currency was USD —
    # Pictet does the FX inside the EFECTO CASH block. The Transaction
    # reflects the *cash-impact* currency, which is what the client's
    # reference account actually moves in.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-53201.29")
    # Quantity / price stay in trade-currency terms; price unit (USD) is
    # implicit, recoverable from ``Precio de ejecución USD`` in the doc.
    assert tx.quantity == Decimal("1296.000")
    assert tx.price == Decimal("44.44")
    assert tx.isin == "LU0128316170"
    assert "Compra" in tx.narration
    assert "AB SICAV" in tx.narration
    assert tx.account_number == "P-999999.999"


def test_suscripcion_template_rejects_compra_doc() -> None:
    """``compra`` (stock purchase) shares ``Tipo de operación: Compra``
    but carries the title ``Compra`` rather than ``Suscripción``; the
    title check is the discriminator."""

    template = PictetSuscripcionTemplate()
    txs = template.extract(_load("compra.txt"))
    assert txs == []
