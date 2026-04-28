from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSwitchSalidaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_switch_salida_template_is_registered() -> None:
    assert "pictet.switch_salida.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.switch_salida.v1"]
    assert template.template_id == "pictet.switch_salida.v1"


def test_switch_salida_extracts_single_transaction() -> None:
    template = PictetSwitchSalidaTemplate()
    txs = template.extract(_load("switch_salida.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 7, 28)
    assert tx.settlement_date == date(2023, 8, 1)
    assert tx.currency == "EUR"
    # Cash-equivalent of the redemption leg, signed positive (the ``sale''
    # side of the switch). No real cash hits a current account — the
    # paired switch_entrada uses the same EUR-equivalent as cost basis.
    assert tx.amount == Decimal("44794.75")
    assert tx.quantity == Decimal("-5.0000")
    assert tx.price == Decimal("8958.95")
    assert tx.isin == "LU1525462542"
    # Switches have no ``Compra``/``Venta`` headline; the narration is
    # synthesised as ``SALIDA <fund>`` from the portfolio block. The
    # word "switch" lives in ``tx.title`` rather than the narration.
    assert tx.title == "Switch"
    assert tx.narration.startswith("SALIDA ")
    assert tx.account_number == "P-999999.999"


def test_switch_salida_rejects_entrada_doc() -> None:
    template = PictetSwitchSalidaTemplate()
    txs = template.extract(_load("switch_entrada.txt"))
    assert txs == []
